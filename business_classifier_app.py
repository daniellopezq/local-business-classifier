"""Standalone GUI for classifying businesses with a local LM Studio model.

The application accepts CSV or XLSX input, preserves every source column, asks
an LM Studio chat model to classify each business name, looks for credible
"time in business" statements on supplied websites, and writes a CSV result.
"""

from __future__ import annotations

import csv
import html
import io
import ipaddress
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import tkinter as tk
import unicodedata
from collections import deque, OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from website_contacts import CONTACT_FIELDS, empty_contacts, research_contacts, owner_messages, context_messages

try:
    import openpyxl
except ImportError:  # CSV input remains available without the optional package.
    openpyxl = None


APP_TITLE = "Local Business Classifier"
DEFAULT_SERVER_URL = "http://localhost:1234"
NONE_COLUMN = "-- None --"
MAX_PAGE_BYTES = 2_000_000
PAGE_TIMEOUT_SECONDS = 12
SESSION_VERSION = 1

MAPPING_FIELDS = (
    ("business_name", "Business name"),
    ("phone", "Phone"),
    ("street", "Street address"),
    ("city_state", "City / state"),
    ("url_1", "URL 1"),
    ("url_2", "URL 2"),
)

AUTO_MAP_ALIASES = {
    "business_name": (
        "business_name",
        "business",
        "company_name",
        "company",
        "name",
        "title",
    ),
    "phone": ("phone", "phone_number", "telephone", "mobile", "hidden"),
    "street": (
        "street",
        "street_address",
        "address",
        "address_1",
        "ml2_divcontainsclass_hidden",
    ),
    "city_state": (
        "city_state",
        "city",
        "location",
        "address_2",
        "ml2",
    ),
    "url_1": (
        "url_1",
        "url1",
        "website_1",
        "website1",
        "title_url",
        "profile_url",
    ),
    "url_2": (
        "url_2",
        "url2",
        "website_2",
        "website2",
        "score_url",
        "business_url",
        "website",
    ),
}

DEDUPE_MAPPING_FIELDS = (
    ("business_name", "Business name"),
    ("address", "Address"),
    ("phone", "Phone"),
)

DEDUPE_AUTO_MAP_ALIASES = {
    "business_name": (
        "normalized_business_name",
        "business_name",
        "business",
        "company_name",
        "company",
        "name",
        "title",
    ),
    "address": (
        "normalized_address",
        "full_address",
        "street_address",
        "address",
        "street",
        "ml2_divcontainsclass_hidden",
    ),
    "phone": (
        "normalized_phone",
        "phone",
        "phone_number",
        "telephone",
        "mobile",
        "hidden",
    ),
}

TOLL_FREE_AREA_CODES = frozenset({"800", "888", "877", "866", "855", "844", "833"})

RESULT_FIELDS = (
    "normalized_business_name",
    "normalized_phone",
    "normalized_address",
    "normalized_url_1",
    "normalized_url_2",
    "classification",
    "classification_reason",
    "classification_confidence",
    "website_checked",
    "founded_year",
    "years_in_business",
    "age_evidence",
    "age_source_url",
    "age_confidence",
    "age_status",
    "processing_error",
)

SOURCE_FIELDS = ("source_file", "source_row")

DIRECTORY_HOST_MARKERS = (
    "manta.com",
    "yelp.com",
    "facebook.com",
    "linkedin.com",
    "angi.com",
    "homeadvisor.com",
    "yellowpages.com",
    "bbb.org",
)


class BusinessClassifierError(RuntimeError):
    """Base error surfaced to the GUI."""


class LMStudioError(BusinessClassifierError):
    """LM Studio connection or response error."""


class LMStudioConnectionError(LMStudioError):
    """LM Studio's HTTP server is not reachable."""


@dataclass(frozen=True)
class ClassificationResult:
    classification: str
    reason: str
    confidence: str


@dataclass(frozen=True)
class AgeFinding:
    founded_year: str = ""
    years_in_business: str = ""
    evidence: str = ""
    confidence: str = ""


@dataclass(frozen=True)
class MappingConfig:
    business_name: str
    phone: str = ""
    street: str = ""
    city_state: str = ""
    url_1: str = ""
    url_2: str = ""


@dataclass(frozen=True)
class ProcessingConfig:
    input_paths: tuple[str, ...]
    output_path: str
    target_business: str
    server_url: str
    model: str
    mapping: MappingConfig
    check_websites: bool
    skip_rejected_websites: bool
    resume_count: int = 0
    pipeline_workers: int = 4
    classification_lookahead: int = 64
    find_email: bool = False
    find_owner: bool = False


@dataclass(frozen=True)
class DeduplicationStats:
    input_rows: int
    kept_rows: int
    duplicate_name_rows: int
    duplicate_address_rows: int
    duplicate_phone_rows: int
    toll_free_rows: int

    @property
    def duplicate_rows(self) -> int:
        return (
            self.duplicate_name_rows
            + self.duplicate_address_rows
            + self.duplicate_phone_rows
        )


def cell_to_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def make_unique_headers(values: Iterable[object]) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}
    for index, value in enumerate(values, start=1):
        base = cell_to_str(value) or f"Column {index}"
        count = seen.get(base, 0) + 1
        seen[base] = count
        headers.append(base if count == 1 else f"{base} [{count}]")
    return headers


def _read_csv(path: str) -> tuple[list[str], list[list[str]]]:
    raw = Path(path).read_bytes()
    decoded = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        raise BusinessClassifierError("The CSV text encoding could not be read.")

    sample = decoded[:8192]
    first_line = decoded.splitlines()[0] if decoded.splitlines() else ""
    # Files produced by this app are comma CSVs and may contain many pipe
    # characters in later error text. Prefer the header's obvious delimiter so
    # Sniffer cannot mistake field content for the table separator.
    delimiter_counts = {delimiter: first_line.count(delimiter) for delimiter in ",\t;|"}
    obvious_delimiter = max(delimiter_counts, key=delimiter_counts.get)
    if delimiter_counts[obvious_delimiter] > 0:
        reader = csv.reader(io.StringIO(decoded, newline=""), delimiter=obvious_delimiter)
    else:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(decoded, newline=""), dialect)
    parsed = list(reader)
    if not parsed:
        raise BusinessClassifierError("The selected file is empty.")
    headers = make_unique_headers(parsed[0])
    rows = [[cell_to_str(value) for value in row] for row in parsed[1:]]
    return headers, rows


def _read_xlsx(path: str) -> tuple[list[str], list[list[str]]]:
    if openpyxl is None:
        raise BusinessClassifierError(
            "Excel input requires openpyxl. Install it with: pip install openpyxl"
        )
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)
        first = next(iterator, None)
        if first is None:
            raise BusinessClassifierError("The selected workbook is empty.")
        headers = make_unique_headers(first)
        rows = [[cell_to_str(value) for value in row] for row in iterator]
        return headers, rows
    finally:
        workbook.close()


def read_tabular_file(path: str) -> tuple[list[str], list[list[str]]]:
    extension = Path(path).suffix.lower()
    if extension == ".csv":
        return _read_csv(path)
    if extension == ".xlsx":
        return _read_xlsx(path)
    raise BusinessClassifierError("Choose a .csv or .xlsx file.")


def read_tabular_files(
    paths: Iterable[str],
) -> tuple[list[str], list[list[str]], list[tuple[str, int]]]:
    """Append same-structure files in selection order and track row provenance."""
    selected = tuple(str(path) for path in paths)
    if not selected:
        raise BusinessClassifierError("Choose at least one .csv or .xlsx file.")

    common_headers: list[str] | None = None
    combined_rows: list[list[str]] = []
    row_sources: list[tuple[str, int]] = []
    first_name = Path(selected[0]).name
    for path in selected:
        headers, rows = read_tabular_file(path)
        if common_headers is None:
            common_headers = headers
        elif headers != common_headers:
            expected = ", ".join(common_headers)
            actual = ", ".join(headers)
            raise BusinessClassifierError(
                f"{Path(path).name} does not have the same columns and order as {first_name}.\n\n"
                f"Expected: {expected}\n\nFound: {actual}"
            )
        combined_rows.extend(rows)
        row_sources.extend((path, row_number) for row_number in range(2, len(rows) + 2))

    return common_headers or [], combined_rows, row_sources


def column_preview(headers: list[str], rows: list[list[str]], selected: str) -> str:
    if selected not in headers:
        return "Choose a column from the loaded file."
    index = headers.index(selected)
    values = [str(row[index]).strip() for row in rows if index < len(row) and str(row[index]).strip()]
    if not values:
        return f"EMPTY — 0/{len(rows):,} rows populated"
    sample = " ".join(values[0].split())[:100]
    number = index + 1
    letter = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letter = chr(65 + remainder) + letter
    return f"Column {letter}: {len(values):,}/{len(rows):,} populated — e.g. {sample}"


def suggest_columns_from_data(headers: list[str], rows: list[list[str]]) -> dict[str, str]:
    """Conservative suggestions, never rename source headers or assume scraper names."""
    mapping = {key: "" for key, _label in MAPPING_FIELDS}
    if not rows:
        return mapping
    scores: dict[str, list[tuple[float, str]]] = {key: [] for key in mapping}
    for index, header in enumerate(headers):
        values = [str(row[index]).strip() for row in rows
                  if index < len(row) and str(row[index]).strip()]
        if not values:
            continue
        coverage = len(values) / len(rows)
        # Evenly sample populated values, not just the first section of a file.
        sample = values[::max(1, len(values) // 200)][:201]
        norm = normalize_header(header)
        base = re.sub(r"[_\d]+$", "", norm)
        is_url = lambda v: bool(re.match(r"^(?:https?://|www\.)", v, re.I))
        is_phone = lambda v: bool(re.fullmatch(r"[+\d\s().-]+", v)) and 10 <= len(re.sub(r"\D", "", v)) <= 15
        is_street = lambda v: bool(re.match(r"^\d+\s+\S+", v)) and not is_phone(v)
        is_city = lambda v: bool(re.fullmatch(r"[\w .'-]+,\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?", v))
        def name_like(value: str) -> bool:
            return (2 <= len(value) <= 100 and any(c.isalpha() for c in value)
                    and len(value.split()) <= 12 and not any(test(value) for test in (is_url, is_phone, is_street, is_city))
                    and value.lower() not in {"visit website", "claimed", "unclaimed", "call now", "contact us"}
                    and not value.lower().startswith(("categorized under", "we ", "our ", "at ")))
        tests = {"business_name": name_like, "phone": is_phone, "street": is_street, "city_state": is_city}
        for key, test in tests.items():
            quality = sum(bool(test(v)) for v in sample) / len(sample)
            if quality < .8:
                continue
            alias = norm in AUTO_MAP_ALIASES[key] or base in AUTO_MAP_ALIASES[key]
            if key == "business_name" and not alias:
                # Repeated category/status text is not a reliable business-name column.
                if len(set(sample)) < 2 or len(set(sample)) / len(sample) < .5:
                    continue
            scores[key].append((quality * (.4 + .6 * coverage) + (.15 if alias else 0), header))
        url_values = [v for v in sample if is_url(v)]
        if len(url_values) / len(sample) >= .8:
            direct = sum(not _is_directory_url(extract_redirect_target(v)) for v in url_values) / len(url_values)
            scores["url_1"].append((coverage + .4 * direct, header))
    used: set[str] = set()
    for key in ("phone", "street", "city_state", "business_name", "url_1", "url_2"):
        ranked = sorted((item for item in scores["url_1" if key == "url_2" else key]
                         if item[1] not in used), reverse=True)
        if not ranked:
            continue
        # Unclear text fields stay unselected for the user to choose from previews.
        if key == "business_name" and (ranked[0][0] < .65 or
                (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < .12)):
            continue
        mapping[key] = ranked[0][1]
        used.add(ranked[0][1])
    return mapping


def mapping_warnings(headers: list[str], rows: list[list[str]], mapping: MappingConfig,
                     check_websites: bool) -> list[str]:
    warnings = []
    if not rows:
        return warnings
    for key, label in (("business_name", "Business name"), ("phone", "Phone")):
        selected = getattr(mapping, key)
        if not selected:
            continue
        missing = sum(not row_value(row, headers, selected).strip() for row in rows)
        if missing / len(rows) > .05:
            warnings.append(f"{label} ({selected}): {missing:,}/{len(rows):,} rows are blank.")
    if check_websites:
        missing = sum(not any(row_value(row, headers, col).strip()
                             for col in (mapping.url_1, mapping.url_2)) for row in rows)
        if missing:
            warnings.append(f"No selected website URL for {missing:,}/{len(rows):,} rows.")
    return warnings


def auto_map_columns(headers: list[str], rows: list[list[str]] | None = None) -> dict[str, str]:
    if rows is not None:
        return suggest_columns_from_data(headers, rows)
    normalized = {header: normalize_header(header) for header in headers}
    mapping: dict[str, str] = {key: "" for key, _label in MAPPING_FIELDS}
    used: set[str] = set()

    for key, _label in MAPPING_FIELDS:
        aliases = AUTO_MAP_ALIASES[key]
        for alias in aliases:
            match = next(
                (
                    header
                    for header in headers
                    if header not in used and normalized[header] == alias
                ),
                None,
            )
            if match:
                mapping[key] = match
                used.add(match)
                break

    # A final conservative fallback for otherwise unmapped URL columns.
    for key in ("url_1", "url_2"):
        if mapping[key]:
            continue
        match = next(
            (
                header
                for header in headers
                if header not in used
                and ("url" in normalized[header] or "website" in normalized[header])
            ),
            None,
        )
        if match:
            mapping[key] = match
            used.add(match)
    return mapping


def auto_map_dedupe_columns(headers: list[str]) -> dict[str, str]:
    """Find the best business-name, address, and phone columns for cleanup."""
    normalized = {header: normalize_header(header) for header in headers}
    mapping: dict[str, str] = {key: "" for key, _label in DEDUPE_MAPPING_FIELDS}
    for key, _label in DEDUPE_MAPPING_FIELDS:
        mapping[key] = next(
            (
                header
                for alias in DEDUPE_AUTO_MAP_ALIASES[key]
                for header in headers
                if normalized[header] == alias
            ),
            "",
        )
    return mapping


def normalize_dedupe_text(value: str) -> str:
    """Normalize case, accents, punctuation, and spacing for duplicate matching."""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(re.findall(r"[a-z0-9]+", plain))


def normalize_phone_for_dedupe(value: str) -> str:
    """Return a comparable US phone number while retaining unusual identifiers."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def is_toll_free_phone(value: str) -> bool:
    digits = normalize_phone_for_dedupe(value)
    return len(digits) >= 10 and digits[:3] in TOLL_FREE_AREA_CODES


def deduplicate_rows(
    headers: list[str],
    rows: list[list[str]],
    business_name_column: str,
    address_column: str,
    phone_column: str,
    remove_toll_free: bool = True,
) -> tuple[list[list[str]], DeduplicationStats]:
    """Run independent name, address, and phone passes, keeping the first match."""
    selected = {
        "business_name": business_name_column,
        "address": address_column,
        "phone": phone_column,
    }
    invalid = [column for column in selected.values() if column and column not in headers]
    if invalid:
        raise BusinessClassifierError("A duplicate-removal column is no longer present in the file.")
    if not any(selected.values()):
        raise BusinessClassifierError("Map at least one duplicate-removal column.")

    current_rows: list[list[str]] = []
    toll_free_count = 0

    for row in rows:
        phone = row_value(row, headers, phone_column)
        if remove_toll_free and phone_column and is_toll_free_phone(phone):
            toll_free_count += 1
            continue
        current_rows.append(row)

    removed_by_column = {key: 0 for key in selected}
    normalizers = {
        "business_name": normalize_dedupe_text,
        "address": normalize_dedupe_text,
        "phone": normalize_phone_for_dedupe,
    }
    for key in ("business_name", "address", "phone"):
        column = selected[key]
        if not column:
            continue
        seen: set[str] = set()
        next_rows: list[list[str]] = []
        for row in current_rows:
            value = normalizers[key](row_value(row, headers, column))
            if value and value in seen:
                removed_by_column[key] += 1
                continue
            next_rows.append(row)
            if value:
                seen.add(value)
        current_rows = next_rows

    return current_rows, DeduplicationStats(
        input_rows=len(rows),
        kept_rows=len(current_rows),
        duplicate_name_rows=removed_by_column["business_name"],
        duplicate_address_rows=removed_by_column["address"],
        duplicate_phone_rows=removed_by_column["phone"],
        toll_free_rows=toll_free_count,
    )


def write_csv_file(path: str, headers: list[str], rows: Iterable[list[str]]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def estimate_remaining_seconds(
    started_at: float,
    started_completed: int,
    samples: list[tuple[float, int]],
    current: int,
    total: int,
) -> float | None:
    """Blend whole-run and recent throughput into a stable remaining-time estimate."""
    if not samples or current <= started_completed or current >= total:
        return 0.0 if current >= total else None
    now = samples[-1][0]
    elapsed = now - started_at
    processed = current - started_completed
    if elapsed <= 0 or processed < 2:
        return None
    lifetime_rate = processed / elapsed
    rate = lifetime_rate

    recent_start_time, recent_start_completed = samples[max(0, len(samples) - 31)]
    recent_elapsed = now - recent_start_time
    recent_processed = current - recent_start_completed
    if recent_elapsed > 0 and recent_processed >= 5:
        recent_rate = recent_processed / recent_elapsed
        rate = (recent_rate * 0.7) + (lifetime_rate * 0.3)
    if rate <= 0:
        return None
    return max(0.0, (total - current) / rate)


def format_remaining_time(seconds: float | None) -> str:
    if seconds is None:
        return "calculating…"
    if seconds <= 0:
        return "complete"
    if seconds < 60:
        return "under 1 min"
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes} min"


def row_value(row: list[str], headers: list[str], selected_header: str) -> str:
    if not selected_header:
        return ""
    try:
        index = headers.index(selected_header)
    except ValueError:
        return ""
    return row[index].strip() if index < len(row) else ""


def compose_address(street: str, city_state: str) -> str:
    return ", ".join(value for value in (street.strip(), city_state.strip()) if value)


def normalize_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "://" not in value and re.match(r"^(?:www\.)?[^\s/]+\.[a-z]{2,}", value, re.I):
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    # http.client rejects spaces and control characters in request targets. Keep
    # existing percent escapes intact while encoding unsafe path/query characters.
    if re.search(r"[\s\x00-\x1f\x7f]", parsed.netloc):
        return ""
    safe_path = quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
    safe_query = quote(parsed.query, safe="=&?/:;+,%@[]!$'()*-._~")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, safe_path, safe_query, ""))


def extract_redirect_target(value: str) -> str:
    """Extract common directory redirect query parameters without fetching them."""
    original = normalize_url(value)
    if not original:
        return ""
    parsed = urlparse(original)
    query = parse_qs(parsed.query)
    for key in ("redirect", "url", "target", "destination", "dest"):
        for candidate in query.get(key, []):
            decoded = candidate
            for _ in range(2):
                decoded = unquote(decoded)
            normalized = normalize_url(decoded)
            if normalized:
                return normalized
    return original


def _is_directory_url(value: str) -> bool:
    host = (urlparse(value).hostname or "").lower()
    return any(host == marker or host.endswith("." + marker) for marker in DIRECTORY_HOST_MARKERS)


def prioritize_candidate_urls(values: Iterable[str]) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = extract_redirect_target(value)
        if not candidate:
            continue
        dedupe_key = candidate.rstrip("/").lower()
        if dedupe_key not in seen:
            seen.add(dedupe_key)
            candidates.append(candidate)
    return sorted(candidates, key=lambda url: (1 if _is_directory_url(url) else 0))


def ensure_public_http_url(value: str) -> str:
    normalized = normalize_url(value)
    if not normalized:
        raise BusinessClassifierError("Unsupported or invalid website URL.")
    parsed = urlparse(normalized)
    if parsed.username or parsed.password:
        raise BusinessClassifierError("Website URLs containing credentials are not allowed.")
    hostname = parsed.hostname or ""
    if hostname.lower() == "localhost" or hostname.lower().endswith(".local"):
        raise BusinessClassifierError("Local network website URLs are not allowed.")
    try:
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        addresses = {
            item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or default_port)
        }
    except OSError as exc:
        raise BusinessClassifierError(f"Could not resolve {hostname}: {exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise BusinessClassifierError("Private or local network website URLs are not allowed.")
    return normalized


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        ensure_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_html(url: str, timeout: int = PAGE_TIMEOUT_SECONDS) -> tuple[str, str]:
    safe_url = ensure_public_http_url(url)
    request = Request(
        safe_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "identity",
        },
    )
    opener = build_opener(_SafeRedirectHandler())
    with opener.open(request, timeout=timeout) as response:
        content_type = (response.headers.get_content_type() or "").lower()
        if content_type and content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
            raise BusinessClassifierError(f"Unsupported website content type: {content_type}")
        body = response.read(MAX_PAGE_BYTES + 1)
        if len(body) > MAX_PAGE_BYTES:
            raise BusinessClassifierError("Website page exceeded the 2 MB safety limit.")
        encoding = response.headers.get_content_charset() or "utf-8"
        return body.decode(encoding, errors="replace"), response.geturl()


class _PageTextParser(HTMLParser):
    IGNORED_TAGS = {"script", "style", "noscript", "svg", "canvas", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._ignored_depth = 0
        self._anchor_href = ""
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "a":
            attributes = dict(attrs)
            self._anchor_href = attributes.get("href") or ""
            self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if not self._ignored_depth and tag == "a" and self._anchor_href:
            label = " ".join(self._anchor_parts).strip()
            self.links.append((self._anchor_href, label))
            self._anchor_href = ""
            self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if not cleaned:
            return
        self.text_parts.append(cleaned)
        if self._anchor_href:
            self._anchor_parts.append(cleaned)


def parse_page(html_text: str) -> tuple[str, list[tuple[str, str]]]:
    parser = _PageTextParser()
    parser.feed(html_text)
    text = re.sub(r"\s+", " ", " ".join(parser.text_parts)).strip()
    return html.unescape(text), parser.links


def _evidence_snippet(text: str, start: int, end: int, limit: int = 220) -> str:
    left = max(0, start - 90)
    right = min(len(text), end + 110)
    snippet = re.sub(r"\s+", " ", text[left:right]).strip(" -|,.;")
    if len(snippet) > limit:
        snippet = snippet[: limit - 1].rstrip() + "…"
    return snippet


def extract_age_evidence(
    text: str,
    raw_html: str = "",
    current_year: int | None = None,
) -> AgeFinding | None:
    current_year = current_year or datetime.now().year
    normalized = re.sub(r"\s+", " ", text)

    founding_patterns = (
        (re.compile(r"\b(?:founded|established|opened|started)\b.{0,45}?\b(?P<year>18\d{2}|19\d{2}|20\d{2})\b", re.I), "high"),
        (re.compile(r"\b(?:in business|operating|serving(?: [a-z ,'-]+)?)\s+since\s+(?P<year>18\d{2}|19\d{2}|20\d{2})\b", re.I), "high"),
        (re.compile(r"\bsince\s+(?P<year>18\d{2}|19\d{2}|20\d{2})\b", re.I), "medium"),
    )
    for pattern, confidence in founding_patterns:
        match = pattern.search(normalized)
        if not match:
            continue
        year = int(match.group("year"))
        if 1800 <= year <= current_year:
            return AgeFinding(
                founded_year=str(year),
                years_in_business=str(current_year - year),
                evidence=_evidence_snippet(normalized, match.start(), match.end()),
                confidence=confidence,
            )

    duration_patterns = (
        re.compile(
            r"\b(?:in business|operating|serving(?: [a-z ,'-]+)?)\b.{0,35}?"
            r"\b(?P<qualifier>over|more than)?\s*(?P<years>\d{1,3})\+?\s+years\b",
            re.I,
        ),
        re.compile(
            r"\b(?P<qualifier>over|more than)\s+(?P<years>\d{1,3})\s+years\s+"
            r"(?:in business|serving|of service)\b",
            re.I,
        ),
        re.compile(r"\b(?P<years>\d{1,3})\+\s+years\s+(?:in business|serving)\b", re.I),
    )
    for pattern in duration_patterns:
        match = pattern.search(normalized)
        if not match:
            continue
        years = int(match.group("years"))
        if not 1 <= years <= 200:
            continue
        qualifier = (match.groupdict().get("qualifier") or "").lower()
        plus = "+" if qualifier in {"over", "more than"} or "+" in match.group(0) else ""
        return AgeFinding(
            years_in_business=f"{years}{plus}",
            evidence=_evidence_snippet(normalized, match.start(), match.end()),
            confidence="medium",
        )

    if raw_html:
        match = re.search(
            r'["\']foundingDate["\']\s*:\s*["\'](?P<year>18\d{2}|19\d{2}|20\d{2})',
            raw_html,
            re.I,
        )
        if match:
            year = int(match.group("year"))
            if year <= current_year:
                return AgeFinding(
                    founded_year=str(year),
                    years_in_business=str(current_year - year),
                    evidence=f"Website metadata foundingDate: {year}",
                    confidence="high",
                )
    return None


def find_about_url(base_url: str, links: Iterable[tuple[str, str]]) -> str:
    base_host = (urlparse(base_url).hostname or "").lower()
    for href, label in links:
        marker = f"{href} {label}".lower()
        if not any(word in marker for word in ("about", "history", "our-story", "our story")):
            continue
        candidate = urljoin(base_url, href)
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() == base_host:
            return candidate
    return ""


def website_failure_status(exc: Exception) -> str:
    if isinstance(exc, HTTPError) and exc.code in {401, 403, 429}:
        return "website_blocked"
    return "website_unavailable"


class PageFetchCache:
    """Small, per-run cache; share simultaneous requests without retaining failures."""
    def __init__(self, fetcher, capacity=8):
        self.fetcher = fetcher
        self.capacity = capacity
        self.pages = OrderedDict()
        self.pending = {}
        self.lock = threading.Lock()

    def __call__(self, url):
        key = normalize_url(url) or url
        with self.lock:
            if key in self.pages:
                self.pages.move_to_end(key)
                return self.pages[key]
            future = self.pending.get(key)
            leader = future is None
            if leader:
                future = self.pending[key] = Future()
        if not leader:
            return future.result()
        try:
            page = self.fetcher(url)
            with self.lock:
                # Store final redirect URL as well, avoiding a second download of
                # the canonical homepage when another stage follows its links.
                for alias in dict.fromkeys((key, normalize_url(page[1]) or page[1])):
                    self.pages[alias] = page
                    self.pages.move_to_end(alias)
                while len(self.pages) > self.capacity:
                    self.pages.popitem(last=False)
                future.set_result(page)
            return page
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                self.pending.pop(key, None)


def find_business_age(
    urls: Iterable[str],
    fetcher: Callable[[str], tuple[str, str]] = fetch_html,
) -> dict[str, str]:
    candidates = prioritize_candidate_urls(urls)
    if not candidates:
        return {
            "website_checked": "",
            "founded_year": "",
            "years_in_business": "",
            "age_evidence": "",
            "age_source_url": "",
            "age_confidence": "",
            "age_status": "no_url",
            "error": "",
        }

    errors: list[str] = []
    failures: list[str] = []
    incomplete = False
    loaded_business_site = False
    checked_any = False
    last_checked = ""
    for candidate in candidates[:2]:
        # A working first-party website is enough: do not query a directory just
        # because no age statement was present on that website.
        if loaded_business_site and _is_directory_url(candidate):
            continue
        attempted_url = candidate
        page_loaded = False
        try:
            page_html, final_url = fetcher(candidate)
            page_loaded = True
            checked_any = True
            loaded_business_site = loaded_business_site or not _is_directory_url(final_url)
            last_checked = final_url
            page_text, links = parse_page(page_html)
            finding = extract_age_evidence(page_text, page_html)
            if finding:
                return _age_result(finding, final_url)

            about_url = find_about_url(final_url, links)
            if about_url and about_url.rstrip("/") != final_url.rstrip("/"):
                attempted_url = about_url
                about_html, about_final_url = fetcher(about_url)
                last_checked = about_final_url
                about_text, _links = parse_page(about_html)
                finding = extract_age_evidence(about_text, about_html)
                if finding:
                    return _age_result(finding, about_final_url)
        # A broken third-party page must be a row-level error, never a batch-level
        # failure. This also contains parser/protocol errors outside urllib's usual
        # HTTPError and URLError hierarchy.
        except Exception as exc:
            status = website_failure_status(exc)
            failures.append(status)
            if page_loaded:
                incomplete = True
            errors.append(f"{status}: {attempted_url}: {exc}")

    return {
        "website_checked": last_checked or candidates[0],
        "founded_year": "",
        "years_in_business": "",
        "age_evidence": "",
        "age_source_url": "",
        "age_confidence": "",
        "age_status": (
            "age_check_incomplete" if checked_any and incomplete else
            "not_found" if checked_any else
            failures[0] if failures else "website_unavailable"
        ),
        # A successful alternate source is not a total failure. Keep diagnostics
        # only when none loaded or an About page needed for research failed.
        "error": " | ".join(errors)[:500] if not checked_any or incomplete else "",
    }


def _age_result(finding: AgeFinding, source_url: str) -> dict[str, str]:
    return {
        "website_checked": source_url,
        "founded_year": finding.founded_year,
        "years_in_business": finding.years_in_business,
        "age_evidence": finding.evidence,
        "age_source_url": source_url,
        "age_confidence": finding.confidence,
        "age_status": "found",
        "error": "",
    }


def normalize_server_url(value: str) -> str:
    value = value.strip().rstrip("/")
    for suffix in ("/api/v1", "/v1"):
        if value.lower().endswith(suffix):
            value = value[: -len(suffix)]
            break
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LMStudioError("Enter a valid LM Studio server URL, such as http://localhost:1234")
    return value


def _request_json(url: str, payload: dict | None = None, timeout: int = 30) -> dict | list:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            detail = ""
        raise LMStudioError(f"LM Studio returned HTTP {exc.code}. {detail}".strip()) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise LMStudioConnectionError(
            "Could not reach LM Studio. Start the Local Server in LM Studio's Developer tab. "
            f"Details: {exc}"
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LMStudioError("LM Studio returned an invalid JSON response.") from exc


def _model_entries(payload: dict | list) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    for key in ("models", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def is_local_server_url(server_url: str) -> bool:
    host = (urlparse(normalize_server_url(server_url)).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def find_lms_executable() -> str:
    located = shutil.which("lms") or shutil.which("lms.exe")
    if located:
        return located
    bundled = Path.home() / ".lmstudio" / "bin" / "lms.exe"
    return str(bundled) if bundled.is_file() else ""


def start_local_lmstudio_server(server_url: str) -> None:
    """Start LM Studio's localhost API without exposing it to the LAN."""
    base_url = normalize_server_url(server_url)
    if not is_local_server_url(base_url):
        raise LMStudioError("Automatic server start is available only for localhost.")
    executable = find_lms_executable()
    if not executable:
        raise LMStudioError(
            "LM Studio's lms.exe was not found. Start the Local Server in the Developer tab."
        )
    parsed = urlparse(base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                executable,
                "server",
                "start",
                "--port",
                str(port),
                "--bind",
                "127.0.0.1",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=creation_flags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LMStudioError(f"Could not start LM Studio's local server: {exc}") from exc
    output = " ".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if completed.returncode != 0 and "already running" not in output.lower():
        raise LMStudioError(
            f"LM Studio's server did not start. {output or 'No details were returned.'}"
        )


class LMStudioClient:
    def __init__(self, server_url: str) -> None:
        self.base_url = normalize_server_url(server_url)
        self._inference_lock = threading.Lock()

    def extract_owners(self, pages: list[dict], model: str, business_name: str = '') -> dict:
        return self._contact_json(owner_messages(pages, business_name), model)

    def review_name_contexts(self, contexts: list[dict], model: str) -> dict:
        return self._contact_json(context_messages(contexts), model)

    def _contact_json(self, messages: list[dict], model: str) -> dict:
        with self._inference_lock:
            response = _request_json(self.base_url + "/v1/chat/completions", payload={
                "model": model, "messages": messages, "temperature": 0,
                "max_tokens": 900, "reasoning_effort": "none", "stream": False,
            }, timeout=120)
        try:
            content = response["choices"][0]["message"]["content"].strip()
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
            return json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise LMStudioError("Owner extraction returned invalid JSON") from exc

    def _list_models_once(self) -> list[str]:
        errors: list[LMStudioError] = []
        for endpoint in ("/api/v1/models", "/v1/models"):
            try:
                payload = _request_json(self.base_url + endpoint, timeout=12)
            except LMStudioError as exc:
                errors.append(exc)
                continue
            model_ids: list[str] = []
            for item in _model_entries(payload):
                identifier = item.get("id") or item.get("key") or item.get("model")
                model_type = str(item.get("type") or item.get("object") or "").lower()
                if "embed" in model_type or (identifier and "embed" in str(identifier).lower()):
                    continue
                if identifier and str(identifier) not in model_ids:
                    model_ids.append(str(identifier))
            if model_ids:
                return model_ids
        if errors:
            if all(isinstance(error, LMStudioConnectionError) for error in errors):
                raise LMStudioConnectionError(str(errors[-1]))
            raise LMStudioError(str(errors[-1]))
        raise LMStudioError(
            "LM Studio is reachable but reported no chat models. Finish the model download "
            "or enable Just-In-Time model loading."
        )

    def list_models(self, auto_start_local: bool = True) -> list[str]:
        try:
            return self._list_models_once()
        except LMStudioConnectionError:
            if not auto_start_local or not is_local_server_url(self.base_url):
                raise
        start_local_lmstudio_server(self.base_url)
        last_error: LMStudioError | None = None
        for _attempt in range(10):
            time.sleep(0.25)
            try:
                return self._list_models_once()
            except LMStudioConnectionError as exc:
                last_error = exc
        raise LMStudioConnectionError(
            "LM Studio's server was started but did not become reachable in time. "
            f"Details: {last_error or 'connection timed out'}"
        )

    def _completion(self, model: str, messages: list[dict], structured: bool) -> str:
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 80,
            # Gemma 4 otherwise spends the small output budget on hidden reasoning
            # and can return an empty final answer for this simple classification.
            "reasoning_effort": "none",
            "stream": False,
        }
        if structured:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "business_classification",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "classification": {
                                "type": "string",
                                "enum": ["approved", "review", "rejected"],
                            },
                            "reason": {"type": "string"},
                            "confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                        },
                        "required": ["classification", "reason", "confidence"],
                        "additionalProperties": False,
                    },
                },
            }
        with self._inference_lock:
            response = _request_json(
                self.base_url + "/v1/chat/completions", payload=payload, timeout=120
            )
        if not isinstance(response, dict):
            raise LMStudioError("LM Studio returned an unexpected completion response.")
        try:
            return str(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LMStudioError("LM Studio completion did not contain message text.") from exc

    def classify(self, business_name: str, target_business: str, model: str) -> ClassificationResult:
        messages = [
            {
                "role": "system",
                "content": (
                    "You classify business names for lead filtering using only the name. "
                    "Approved means the name clearly indicates the requested trade. Review means "
                    "the name is ambiguous, broad, a general contractor, or an adjacent trade. "
                    "Rejected means the name clearly identifies an unrelated trade or industry. "
                    "A neutral personal or company name must be Review, not Rejected. Give one "
                    "short reason and do not add facts that are not present in the name."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Target business type: {target_business}\n"
                    f"Business name: {business_name}\n"
                    "Return classification, reason, and confidence."
                ),
            },
        ]
        errors: list[str] = []
        for structured in (True, False):
            try:
                content = self._completion(model, messages, structured=structured)
            except LMStudioError as exc:
                errors.append(str(exc))
                continue
            result = parse_classification_response(content)
            if result:
                return result
            errors.append("The model response did not contain a valid classification.")
        if errors and all("valid classification" not in error for error in errors):
            raise LMStudioError(errors[-1])
        return ClassificationResult(
            classification="review",
            reason="Model response was invalid; sent to review for safety.",
            confidence="low",
        )


def parse_classification_response(content: str) -> ClassificationResult | None:
    cleaned = content.strip()
    cleaned = re.sub(r"```(?:json)?|```", "", cleaned, flags=re.I).strip()
    candidates = re.findall(r"\{.*?\}", cleaned, flags=re.S)
    for candidate in reversed(candidates):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        classification = str(payload.get("classification") or payload.get("status") or "").lower()
        confidence = str(payload.get("confidence") or "medium").lower()
        reason = str(payload.get("reason") or "Classified from the business name.").strip()
        if classification in {"approved", "review", "rejected"}:
            if confidence not in {"high", "medium", "low"}:
                confidence = "medium"
            return ClassificationResult(classification, reason[:240], confidence)

    label_match = re.search(r"\b(approved|review|rejected)\b", cleaned, re.I)
    if not label_match:
        return None
    classification = label_match.group(1).lower()
    confidence_match = re.search(r"confidence\s*[:=-]\s*(high|medium|low)", cleaned, re.I)
    reason_match = re.search(r"reason\s*[:=-]\s*(.+)", cleaned, re.I)
    confidence = confidence_match.group(1).lower() if confidence_match else "medium"
    reason = reason_match.group(1).strip()[:240] if reason_match else "Classified from the business name."
    return ClassificationResult(classification, reason, confidence)


def unique_field_names(headers: Iterable[str], fields: Iterable[str]) -> dict[str, str]:
    used = set(headers)
    result: dict[str, str] = {}
    for field in fields:
        candidate = field
        counter = 2
        while candidate in used:
            candidate = f"{field}_{counter}"
            counter += 1
        used.add(candidate)
        result[field] = candidate
    return result


def unique_result_field_names(headers: list[str]) -> dict[str, str]:
    return unique_field_names(headers, RESULT_FIELDS)


def output_field_layout(
    headers: list[str], include_source_columns: bool, include_contacts: bool = False
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    source_names = (
        unique_field_names(headers, SOURCE_FIELDS) if include_source_columns else {}
    )
    occupied = [*headers, *source_names.values()]
    result_names = unique_result_field_names(occupied)
    if include_contacts:
        result_names.update(unique_field_names([*occupied, *result_names.values()], CONTACT_FIELDS))
    return source_names, result_names, [*occupied, *result_names.values()]


def partial_output_path(output_path: str) -> str:
    path = Path(output_path)
    return str(path.with_name(f"{path.stem}.partial{path.suffix or '.csv'}"))


def compatible_partial_row_count(
    partial_path: str,
    headers: list[str],
    source_rows: list[list[str]],
    row_sources: list[tuple[str, int]] | None = None,
    include_source_columns: bool = False,
    include_contacts: bool = False,
) -> int:
    """Return completed row count only when a partial matches this exact input."""
    path = Path(partial_path)
    if not path.is_file():
        return 0
    source_names, _result_names, expected_headers = output_field_layout(
        headers, include_source_columns, include_contacts
    )
    if include_source_columns and (
        row_sources is None or len(row_sources) != len(source_rows)
    ):
        return 0
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != expected_headers:
                return 0
            completed = 0
            for record in reader:
                if completed >= len(source_rows):
                    return 0
                source = source_rows[completed]
                for position, header in enumerate(headers):
                    expected = source[position] if position < len(source) else ""
                    if (record.get(header) or "") != expected:
                        return 0
                if include_source_columns and row_sources is not None:
                    source_path, source_row = row_sources[completed]
                    if (record.get(source_names["source_file"]) or "") != Path(source_path).name:
                        return 0
                    if (record.get(source_names["source_row"]) or "") != str(source_row):
                        return 0
                completed += 1
            return completed
    except (OSError, csv.Error, UnicodeError):
        return 0


def default_session_path() -> Path:
    """Legacy single-session location retained for backward-compatible discovery."""
    base = Path(os.getenv("LOCALAPPDATA") or Path.home())
    return base / "LocalBusinessClassifier" / "current_session.json"


def default_session_directory() -> Path:
    base = Path(os.getenv("LOCALAPPDATA") or Path.home())
    return base / "LocalBusinessClassifier" / "sessions"


def new_session_path(output_path: str, directory: str | Path | None = None) -> Path:
    folder = Path(directory) if directory is not None else default_session_directory()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(output_path).stem).strip("_")
    return folder / f"{stamp}_{stem or 'classifier'}.json"


def save_session_state(path: str | Path, payload: dict) -> None:
    """Atomically persist a small JSON session file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    encoded = json.dumps(payload, indent=2, ensure_ascii=False)
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, destination)


def load_session_state(path: str | Path) -> dict | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != SESSION_VERSION:
        return None
    if not isinstance(payload.get("input_paths"), list) or not payload.get("output_path"):
        return None
    return payload


def list_session_states(
    directory: str | Path | None = None,
    include_legacy: bool = True,
    limit: int = 3,
) -> list[tuple[Path, dict]]:
    folder = Path(directory) if directory is not None else default_session_directory()
    candidates = list(folder.glob("*.json")) if folder.is_dir() else []
    if include_legacy:
        legacy = default_session_path()
        if legacy.is_file() and legacy not in candidates:
            candidates.append(legacy)
    records: list[tuple[Path, dict]] = []
    for path in candidates:
        payload = load_session_state(path)
        if payload:
            records.append((path, payload))
    records.sort(
        key=lambda record: (
            str(record[1].get("updated_at") or ""),
            record[0].stat().st_mtime if record[0].exists() else 0,
        ),
        reverse=True,
    )
    return records[: max(0, limit)]


def prune_session_states(directory: str | Path | None = None, keep: int = 3) -> None:
    folder = Path(directory) if directory is not None else default_session_directory()
    if not folder.is_dir():
        return
    paths = sorted(
        folder.glob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in paths[max(0, keep) :]:
        try:
            path.unlink()
        except OSError:
            pass


def session_payload(
    config: ProcessingConfig,
    completed: int,
    total: int,
    status: str,
    error: str = "",
) -> dict:
    return {
        "version": SESSION_VERSION,
        "status": status,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "input_paths": list(config.input_paths),
        "output_path": config.output_path,
        "partial_output_path": partial_output_path(config.output_path),
        "target_business": config.target_business,
        "server_url": config.server_url,
        "model": config.model,
        "mapping": asdict(config.mapping),
        "check_websites": config.check_websites,
        "find_email": config.find_email,
        "find_owner": config.find_owner,
        "skip_rejected_websites": config.skip_rejected_websites,
        "pipeline_workers": config.pipeline_workers,
        "classification_lookahead": config.classification_lookahead,
        "completed_rows": completed,
        "total_rows": total,
        "error": error[:1000],
    }


class BusinessClassifierApp:
    def __init__(self, root: tk.Tk, offer_session_restore: bool = True) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("980x760")
        self.root.minsize(860, 680)

        self.events: queue.Queue[dict] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.session_path: Path | None = None
        self.session_status = ""
        self._session_warning_sent = False
        self.input_paths: tuple[str, ...] = ()
        self.headers: list[str] = []
        self.rows: list[list[str]] = []
        self.row_sources: list[tuple[str, int]] = []
        self.mapping_vars: dict[str, tk.StringVar] = {}
        self.mapping_boxes: dict[str, ttk.Combobox] = {}
        self.dedupe_headers: list[str] = []
        self.dedupe_rows: list[list[str]] = []
        self.dedupe_mapping_vars: dict[str, tk.StringVar] = {}
        self.dedupe_mapping_boxes: dict[str, ttk.Combobox] = {}

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.target_var = tk.StringVar(value="Roofing contractors")
        self.server_var = tk.StringVar(value=DEFAULT_SERVER_URL)
        self.model_var = tk.StringVar()
        self.check_websites_var = tk.BooleanVar(value=True)
        self.find_email_var = tk.BooleanVar(value=False)
        self.find_owner_var = tk.BooleanVar(value=False)
        self.skip_rejected_var = tk.BooleanVar(value=True)
        self.pipeline_workers_var = tk.IntVar(value=4)
        self.classification_lookahead_var = tk.IntVar(value=64)
        self.status_var = tk.StringVar(value="Choose one or more input files to begin.")
        self.progress_var = tk.DoubleVar(value=0)
        self.eta_var = tk.StringVar(value="Estimated time remaining: --")
        self._eta_started_at: float | None = None
        self._eta_started_completed = 0
        self._eta_samples: deque[tuple[float, int]] = deque(maxlen=61)
        self.dedupe_input_var = tk.StringVar()
        self.dedupe_output_var = tk.StringVar()
        self.dedupe_remove_toll_free_var = tk.BooleanVar(value=True)
        self.dedupe_auto_after_run_var = tk.BooleanVar(value=True)
        self.dedupe_status_var = tk.StringVar(value="Choose a classified CSV or Excel file.")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_events)
        if offer_session_restore:
            self.root.after(500, self._offer_session_restore)

    def _build_ui(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)
        self.classifier_tab = ttk.Frame(self.notebook)
        self.dedupe_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.classifier_tab, text="Business Classifier")
        self.notebook.add(self.dedupe_tab, text="Duplicate Removal")

        outer = ttk.Frame(self.classifier_tab, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)

        ttk.Label(outer, text=APP_TITLE, font=("Segoe UI", 17, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )

        ttk.Label(outer, text="Input files").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.input_var).grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(outer, text="Browse files…", command=self._browse_input).grid(row=1, column=2)

        ttk.Label(outer, text="Output CSV").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.output_var).grid(row=2, column=1, sticky="ew", padx=8)
        ttk.Button(outer, text="Save as…", command=self._browse_output).grid(row=2, column=2)

        ttk.Label(outer, text="Target business").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.target_var).grid(row=3, column=1, columnspan=2, sticky="ew", padx=8)

        server_frame = ttk.LabelFrame(outer, text="LM Studio", padding=10)
        server_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 8))
        server_frame.columnconfigure(1, weight=1)
        ttk.Label(server_frame, text="Server URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(server_frame, textvariable=self.server_var, width=34).grid(
            row=0, column=1, sticky="ew", padx=8
        )
        self.connect_button = ttk.Button(server_frame, text="Connect / Refresh", command=self._connect_models)
        self.connect_button.grid(row=0, column=2)
        ttk.Label(server_frame, text="Model").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.model_box = ttk.Combobox(server_frame, textvariable=self.model_var, state="readonly")
        self.model_box.grid(row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=(8, 0))

        mapping_frame = ttk.LabelFrame(outer, text="Column mapping", padding=10)
        mapping_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=8)
        mapping_frame.columnconfigure(1, weight=1)
        for index, (key, label) in enumerate(MAPPING_FIELDS):
            ttk.Label(mapping_frame, text=label).grid(row=index, column=0, sticky="w", pady=2)
            variable = tk.StringVar(value=NONE_COLUMN)
            box = ttk.Combobox(mapping_frame, textvariable=variable, state="readonly")
            box.grid(row=index, column=1, sticky="ew", padx=(10, 0), pady=2)
            self.mapping_vars[key] = variable
            self.mapping_boxes[key] = box
            preview = ttk.Label(mapping_frame, text="Choose a column from the loaded file.", width=42, wraplength=300)
            preview.grid(row=index, column=2, sticky="w", padx=(10, 0), pady=2)
            variable.trace_add(
                "write",
                lambda *_args, var=variable, label=preview: label.configure(
                    text=column_preview(self.headers, self.rows, var.get())
                ),
            )

        ttk.Label(mapping_frame, text="Suggestions use this file's contents. Check samples before starting.").grid(
            row=len(MAPPING_FIELDS), column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(mapping_frame, text="Re-detect columns", command=self._redetect_columns).grid(
            row=len(MAPPING_FIELDS), column=2, sticky="w", pady=(6, 0))

        options = ttk.Frame(outer)
        options.grid(row=6, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Checkbutton(
            options, text="Check websites for business age", variable=self.check_websites_var
        ).pack(side="left")
        ttk.Checkbutton(
            options,
            text="Skip website checks for rejected businesses",
            variable=self.skip_rejected_var,
        ).pack(side="left", padx=(20, 0))
        ttk.Label(options, text="Website workers").pack(
            side="left", padx=(20, 6)
        )
        ttk.Combobox(
            options,
            textvariable=self.pipeline_workers_var,
            values=(1, 2, 4, 8),
            state="readonly",
            width=3,
        ).pack(side="left")

        ttk.Label(options, text="Classify ahead").pack(side="left", padx=(12, 6))
        ttk.Combobox(
            options, textvariable=self.classification_lookahead_var,
            values=(16, 32, 64, 128), state="readonly", width=4,
        ).pack(side="left")

        actions = ttk.Frame(outer)
        actions.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        research_options = ttk.Frame(actions)
        research_options.pack(side="top", fill="x", pady=(0, 8))
        ttk.Checkbutton(research_options, text="Find website email (owner preferred)",
                        variable=self.find_email_var).pack(side="left")
        ttk.Checkbutton(research_options, text="Find owner/contact names (includes unconfirmed)",
                        variable=self.find_owner_var).pack(side="left", padx=12)
        self.start_button = ttk.Button(actions, text="Start", command=self._start_processing)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="Stop", command=self._stop_processing, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="Restore Session…", command=self._show_session_picker).pack(
            side="left", padx=(12, 0)
        )

        self.progress = ttk.Progressbar(outer, variable=self.progress_var, maximum=100)
        self.progress.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(6, 2))
        ttk.Label(
            outer,
            textvariable=self.eta_var,
            font=("Segoe UI", 10, "bold"),
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Label(outer, textvariable=self.status_var).grid(
            row=10, column=0, columnspan=3, sticky="w"
        )

        log_frame = ttk.LabelFrame(outer, text="Activity", padding=6)
        log_frame.grid(row=11, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        outer.rowconfigure(11, weight=1)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=10, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

        self._build_dedupe_ui()

    def _build_dedupe_ui(self) -> None:
        outer = ttk.Frame(self.dedupe_tab, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)

        ttk.Label(outer, text="Duplicate Removal", font=("Segoe UI", 17, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            outer,
            text=(
                "Runs separate passes by business name, then address, then phone. "
                "Each pass keeps the first nonblank value and removes later matches, "
                "so different names at one address and one name with different phones "
                "collapse correctly. Source columns are preserved."
            ),
            wraplength=860,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 14))

        ttk.Label(outer, text="Input file").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.dedupe_input_var).grid(
            row=2, column=1, sticky="ew", padx=8
        )
        ttk.Button(outer, text="Browse…", command=self._browse_dedupe_input).grid(
            row=2, column=2
        )

        ttk.Label(outer, text="Output CSV").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.dedupe_output_var).grid(
            row=3, column=1, sticky="ew", padx=8
        )
        ttk.Button(outer, text="Save as…", command=self._browse_dedupe_output).grid(
            row=3, column=2
        )

        mapping_frame = ttk.LabelFrame(outer, text="Duplicate matching columns", padding=10)
        mapping_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 8))
        mapping_frame.columnconfigure(1, weight=1)
        for index, (key, label) in enumerate(DEDUPE_MAPPING_FIELDS):
            ttk.Label(mapping_frame, text=label).grid(row=index, column=0, sticky="w", pady=3)
            variable = tk.StringVar(value=NONE_COLUMN)
            box = ttk.Combobox(mapping_frame, textvariable=variable, state="readonly")
            box.grid(row=index, column=1, sticky="ew", padx=(12, 0), pady=3)
            self.dedupe_mapping_vars[key] = variable
            self.dedupe_mapping_boxes[key] = box

        ttk.Checkbutton(
            outer,
            text="Remove toll-free phone numbers (800, 888, 877, 866, 855, 844, 833)",
            variable=self.dedupe_remove_toll_free_var,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 12))

        ttk.Checkbutton(
            outer,
            text="Automatically remove duplicates when a classification run finishes",
            variable=self.dedupe_auto_after_run_var,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 12))

        self.dedupe_run_button = ttk.Button(
            outer, text="Remove Duplicates", command=self._run_deduplication
        )
        self.dedupe_run_button.grid(row=7, column=0, columnspan=3, sticky="w")
        ttk.Label(outer, textvariable=self.dedupe_status_var, wraplength=860).grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(14, 0)
        )

    def _browse_dedupe_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a classified business file",
            filetypes=[("CSV or Excel", "*.csv *.xlsx"), ("CSV", "*.csv"), ("Excel", "*.xlsx")],
        )
        if path:
            self._load_dedupe_file(path, select_tab=True)

    def _load_dedupe_file(self, path: str, select_tab: bool = False) -> bool:
        try:
            headers, rows = read_tabular_file(path)
        except (BusinessClassifierError, OSError) as exc:
            messagebox.showerror(APP_TITLE, f"Could not load the cleanup file:\n\n{exc}")
            return False

        self.dedupe_headers = headers
        self.dedupe_rows = rows
        self.dedupe_input_var.set(path)
        source = Path(path)
        self.dedupe_output_var.set(str(source.with_name(f"{source.stem}_deduplicated.csv")))
        detected = auto_map_dedupe_columns(headers)
        choices = [NONE_COLUMN, *headers]
        for key, _label in DEDUPE_MAPPING_FIELDS:
            self.dedupe_mapping_boxes[key]["values"] = choices
            self.dedupe_mapping_vars[key].set(detected.get(key) or NONE_COLUMN)
        self.dedupe_status_var.set(
            f"Loaded {len(rows):,} rows. Review the mapped columns, then remove duplicates."
        )
        if select_tab:
            self.notebook.select(self.dedupe_tab)
        return True

    def _browse_dedupe_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save deduplicated CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=(
                Path(self.dedupe_output_var.get()).name
                if self.dedupe_output_var.get()
                else "deduplicated.csv"
            ),
        )
        if path:
            self.dedupe_output_var.set(path)

    def _run_deduplication(self) -> None:
        input_path = self.dedupe_input_var.get().strip()
        output_path = self.dedupe_output_var.get().strip()
        if not input_path or not self.dedupe_headers:
            messagebox.showerror(APP_TITLE, "Choose a CSV or Excel file first.")
            return
        if not output_path:
            messagebox.showerror(APP_TITLE, "Choose an output CSV path.")
            return
        if os.path.abspath(input_path) == os.path.abspath(output_path):
            messagebox.showerror(APP_TITLE, "The cleanup output must differ from the input file.")
            return
        if Path(output_path).exists() and not messagebox.askyesno(
            APP_TITLE, f"Replace the existing output file?\n\n{output_path}"
        ):
            return

        selected = {
            key: "" if variable.get() == NONE_COLUMN else variable.get()
            for key, variable in self.dedupe_mapping_vars.items()
        }
        self.dedupe_run_button.configure(state="disabled")
        self.dedupe_status_var.set("Removing duplicate and toll-free rows…")
        self.root.update_idletasks()
        try:
            headers, rows = read_tabular_file(input_path)
            kept, stats = deduplicate_rows(
                headers,
                rows,
                selected["business_name"],
                selected["address"],
                selected["phone"],
                remove_toll_free=self.dedupe_remove_toll_free_var.get(),
            )
            write_csv_file(output_path, headers, kept)
        except (BusinessClassifierError, OSError, csv.Error) as exc:
            messagebox.showerror(APP_TITLE, f"Duplicate removal stopped:\n\n{exc}")
            self.dedupe_status_var.set(f"Duplicate removal stopped: {exc}")
        else:
            message = (
                f"Finished: kept {stats.kept_rows:,} of {stats.input_rows:,} rows; "
                f"removed {stats.duplicate_name_rows:,} by name, "
                f"{stats.duplicate_address_rows:,} by address, "
                f"{stats.duplicate_phone_rows:,} by phone, and "
                f"{stats.toll_free_rows:,} toll-free rows.\n\nSaved to:\n{output_path}"
            )
            self.dedupe_status_var.set(message.replace("\n", " "))
            messagebox.showinfo(APP_TITLE, message)
        finally:
            self.dedupe_run_button.configure(state="normal")

    def _browse_input(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose one or more business lists",
            filetypes=[("CSV or Excel", "*.csv *.xlsx"), ("CSV", "*.csv"), ("Excel", "*.xlsx")],
        )
        if not paths:
            return
        try:
            headers, rows, row_sources = read_tabular_files(paths)
        except BusinessClassifierError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.input_paths = tuple(paths)
        self.session_path = None
        self.session_status = ""
        self.headers = headers
        self.rows = rows
        self.row_sources = row_sources
        if len(paths) == 1:
            display = paths[0]
            output_name = f"{Path(paths[0]).stem}_classified.csv"
        else:
            display = f"{len(paths)} files: {Path(paths[0]).name} (+{len(paths) - 1} more)"
            output_name = "combined_classified.csv"
        self.input_var.set(display)
        self.output_var.set(str(Path(paths[0]).with_name(output_name)))
        detected = auto_map_columns(self.headers, self.rows)
        choices = [NONE_COLUMN, *self.headers]
        for key, _label in MAPPING_FIELDS:
            self.mapping_boxes[key]["values"] = choices
            self.mapping_vars[key].set(detected.get(key) or NONE_COLUMN)
        self.status_var.set(
            f"Loaded {len(self.rows):,} rows from {len(paths)} file(s); "
            f"{len(self.headers)} common columns."
        )
        self.eta_var.set("Estimated time remaining: --")
        for path in paths:
            self._write_log(f"Loaded {path}")
        self._write_log(
            f"Combined rows: {len(self.rows):,}; files: {len(paths)}; "
            f"columns: {len(self.headers)}"
        )

    def _browse_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save classified CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=Path(self.output_var.get()).name if self.output_var.get() else "classified.csv",
        )
        if path:
            self.output_var.set(path)

    def _offer_session_restore(self) -> None:
        records = list_session_states()
        record = next(
            (
                item
                for item in records
                if item[1].get("status") != "complete"
            ),
            None,
        )
        if not record:
            latest_complete = next(
                (
                    payload
                    for _path, payload in records
                    if payload.get("status") == "complete"
                    and Path(str(payload.get("output_path") or "")).is_file()
                ),
                None,
            )
            if latest_complete:
                self._load_dedupe_file(
                    str(latest_complete["output_path"]), select_tab=True
                )
            return
        path, payload = record
        completed = int(payload.get("completed_rows") or 0)
        total = int(payload.get("total_rows") or 0)
        updated = str(payload.get("updated_at") or "unknown time")
        if messagebox.askyesno(
            APP_TITLE,
            "An unfinished classifier session was found.\n\n"
            f"Saved: {updated}\nProgress: {completed:,} of {total:,} rows\n\n"
            "Restore its files and settings?",
        ):
            self._restore_session(path, payload)

    def _show_session_picker(self) -> None:
        records = list_session_states()
        if not records:
            messagebox.showinfo(APP_TITLE, "No saved classifier sessions were found.")
            return
        if len(records) == 1:
            self._restore_session(*records[0])
            return

        window = tk.Toplevel(self.root)
        window.title("Restore Classifier Session")
        window.transient(self.root)
        window.grab_set()
        window.geometry("780x260")
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Choose one of the last three sessions:").pack(anchor="w")
        columns = ("updated", "status", "progress", "output")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=5)
        for name, label, width in (
            ("updated", "Last saved", 175),
            ("status", "Status", 85),
            ("progress", "Progress", 100),
            ("output", "Output", 360),
        ):
            tree.heading(name, text=label)
            tree.column(name, width=width, anchor="w")
        tree.pack(fill="both", expand=True, pady=(8, 10))
        indexed: dict[str, tuple[Path, dict]] = {}
        for number, record in enumerate(records):
            path, payload = record
            item_id = f"session-{number}"
            indexed[item_id] = record
            completed = int(payload.get("completed_rows") or 0)
            total = int(payload.get("total_rows") or 0)
            tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    str(payload.get("updated_at") or "").replace("T", " ")[:19],
                    str(payload.get("status") or "unknown").title(),
                    f"{completed:,} / {total:,}",
                    Path(str(payload.get("output_path") or "")).name,
                ),
            )
        tree.selection_set("session-0")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")

        def restore_selected() -> None:
            selection = tree.selection()
            if not selection:
                return
            record = indexed[selection[0]]
            window.destroy()
            self._restore_session(*record)

        ttk.Button(buttons, text="Restore", command=restore_selected).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(
            side="right", padx=(0, 8)
        )
        tree.bind("<Double-1>", lambda _event: restore_selected())

    def _restore_session(self, session_path: Path, payload: dict) -> None:
        if payload.get("status") == "complete" and not messagebox.askyesno(
            APP_TITLE,
            "This session completed successfully. Restore its settings for a new run?",
        ):
            return
        paths = tuple(str(path) for path in payload.get("input_paths") or ())
        missing = [path for path in paths if not Path(path).is_file()]
        if missing:
            messagebox.showerror(
                APP_TITLE,
                "The saved session cannot be restored because these input files are missing:\n\n"
                + "\n".join(missing[:8]),
            )
            return
        try:
            headers, rows, row_sources = read_tabular_files(paths)
        except BusinessClassifierError as exc:
            messagebox.showerror(APP_TITLE, f"Could not restore the saved session:\n\n{exc}")
            return

        self.input_paths = paths
        self.session_path = session_path if payload.get("status") != "complete" else None
        self.session_status = str(payload.get("status") or "")
        self.headers = headers
        self.rows = rows
        self.row_sources = row_sources
        if len(paths) == 1:
            self.input_var.set(paths[0])
        else:
            self.input_var.set(
                f"{len(paths)} files: {Path(paths[0]).name} (+{len(paths) - 1} more)"
            )
        self.output_var.set(str(payload.get("output_path") or ""))
        self.target_var.set(str(payload.get("target_business") or "Roofing contractors"))
        self.server_var.set(str(payload.get("server_url") or DEFAULT_SERVER_URL))
        self.check_websites_var.set(bool(payload.get("check_websites", True)))
        self.find_email_var.set(bool(payload.get("find_email", False)))
        self.find_owner_var.set(bool(payload.get("find_owner", False)))
        self.skip_rejected_var.set(bool(payload.get("skip_rejected_websites", True)))
        self.pipeline_workers_var.set(
            max(1, min(8, int(payload.get("pipeline_workers") or 4)))
        )
        self.classification_lookahead_var.set(
            max(1, min(128, int(payload.get("classification_lookahead") or 64)))
        )

        detected = auto_map_columns(headers, rows)
        saved_mapping = payload.get("mapping") if isinstance(payload.get("mapping"), dict) else {}
        choices = [NONE_COLUMN, *headers]
        for key, _label in MAPPING_FIELDS:
            self.mapping_boxes[key]["values"] = choices
            selected = str(saved_mapping.get(key) or detected.get(key) or "")
            self.mapping_vars[key].set(selected if selected in headers else NONE_COLUMN)

        saved_model = str(payload.get("model") or "")
        if saved_model:
            self.model_box["values"] = [saved_model]
            self.model_var.set(saved_model)
        include_source_columns = len(paths) > 1
        completed = compatible_partial_row_count(
            partial_output_path(self.output_var.get()),
            headers,
            rows,
            row_sources=row_sources,
            include_source_columns=include_source_columns,
            include_contacts=self.find_email_var.get() or self.find_owner_var.get(),
        )
        self.progress_var.set(completed / max(1, len(rows)) * 100)
        remaining = max(0, len(rows) - completed)
        self.eta_var.set(
            f"Estimated time remaining: ready to calculate ({remaining:,} rows remaining)"
        )
        self.status_var.set(
            f"Session restored: {completed:,} of {len(rows):,} completed rows are resumable."
        )
        self._write_log(f"Restored session from {session_path}")
        self._write_log(
            f"Inputs: {len(paths)} file(s); resumable rows: {completed:,}/{len(rows):,}"
        )
        self._connect_models()

    def _persist_session(
        self,
        config: ProcessingConfig,
        completed: int,
        status: str,
        error: str = "",
    ) -> None:
        try:
            if self.session_path is None:
                self.session_path = new_session_path(config.output_path)
            save_session_state(
                self.session_path,
                session_payload(config, completed, len(self.rows), status, error),
            )
            self.session_status = status
            prune_session_states(self.session_path.parent, keep=3)
        except OSError as exc:
            if not self._session_warning_sent:
                self._session_warning_sent = True
                self.events.put(
                    {"kind": "log", "message": f"Warning: session state could not be saved: {exc}"}
                )

    def _connect_models(self) -> None:
        self.connect_button.configure(state="disabled")
        self.status_var.set("Connecting to LM Studio…")

        def work() -> None:
            try:
                models = LMStudioClient(self.server_var.get()).list_models()
                self.events.put({"kind": "models", "models": models})
            except Exception as exc:
                self.events.put({"kind": "error", "message": str(exc), "context": "connect"})

        threading.Thread(target=work, daemon=True).start()

    def _redetect_columns(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_TITLE, "Stop the current run before changing its mappings.")
            return
        detected = auto_map_columns(self.headers, self.rows)
        for key, variable in self.mapping_vars.items():
            variable.set(detected.get(key) or NONE_COLUMN)

    def _mapping_config(self) -> MappingConfig:
        values = {
            key: "" if variable.get() == NONE_COLUMN else variable.get()
            for key, variable in self.mapping_vars.items()
        }
        return MappingConfig(**values)

    def _start_processing(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.headers or not self.input_paths:
            messagebox.showerror(APP_TITLE, "Choose and load one or more input files first.")
            return
        mapping = self._mapping_config()
        if not mapping.business_name:
            messagebox.showerror(APP_TITLE, "Map the Business name column.")
            return
        if mapping.business_name not in self.headers or not any(
            row_value(row, self.headers, mapping.business_name).strip() for row in self.rows
        ):
            messagebox.showerror(
                APP_TITLE,
                "The selected Business name column is missing or entirely empty.\n"
                "Choose a populated column using the sample values beside the dropdowns.",
            )
            return
        warnings = mapping_warnings(self.headers, self.rows, mapping,
                                    self.check_websites_var.get() or self.find_email_var.get() or self.find_owner_var.get())
        if warnings and not messagebox.askyesno(
            APP_TITLE, "Check column mappings before starting:\n\n" + "\n".join(warnings)
            + "\n\nBlank names will skip AI classification. Continue anyway?", default="no"
        ):
            return
        if not self.target_var.get().strip():
            messagebox.showerror(APP_TITLE, "Enter a target business type.")
            return
        if not self.model_var.get().strip():
            messagebox.showerror(APP_TITLE, "Connect to LM Studio and select a model.")
            return
        output_path = self.output_var.get().strip()
        if not output_path:
            messagebox.showerror(APP_TITLE, "Choose an output CSV path.")
            return
        input_absolute_paths = {os.path.abspath(path) for path in self.input_paths}
        if os.path.abspath(output_path) in input_absolute_paths:
            messagebox.showerror(APP_TITLE, "The output path must be different from the input file.")
            return

        include_source_columns = len(self.input_paths) > 1
        resume_count = compatible_partial_row_count(
            partial_output_path(output_path),
            self.headers,
            self.rows,
            row_sources=self.row_sources,
            include_source_columns=include_source_columns,
            include_contacts=self.find_email_var.get() or self.find_owner_var.get(),
        )
        if Path(partial_output_path(output_path)).exists() and not resume_count:
            messagebox.showerror(APP_TITLE,
                "This output already has a partial file that does not match the current input/options.\n"
                "Choose a new output filename to preserve the earlier work.")
            return
        if resume_count and (self.find_email_var.get() or self.find_owner_var.get()):
            saved = next((payload for _path, payload in list_session_states()
                          if os.path.normcase(os.path.abspath(str(payload.get("output_path") or "")))
                          == os.path.normcase(os.path.abspath(output_path))), None)
            if saved is None or (
                bool(saved.get("find_email", False)) != self.find_email_var.get()
                or bool(saved.get("find_owner", False)) != self.find_owner_var.get()
            ):
                messagebox.showerror(APP_TITLE,
                    "The partial's contact-research options differ or could not be verified.\n"
                    "Restore its saved options or choose a new output filename.")
                return
        if resume_count and not messagebox.askyesno(
            APP_TITLE,
            f"A compatible partial output contains {resume_count} completed rows.\n\n"
            "Resume from the next row? Choose No to replace the partial output and start over.",
        ):
            resume_count = 0

        config = ProcessingConfig(
            input_paths=self.input_paths,
            output_path=output_path,
            target_business=self.target_var.get().strip(),
            server_url=self.server_var.get().strip(),
            model=self.model_var.get().strip(),
            mapping=mapping,
            check_websites=self.check_websites_var.get(),
            find_email=self.find_email_var.get(),
            find_owner=self.find_owner_var.get(),
            skip_rejected_websites=self.skip_rejected_var.get(),
            resume_count=resume_count,
            pipeline_workers=max(1, min(8, int(self.pipeline_workers_var.get()))),
            classification_lookahead=max(1, min(128, int(self.classification_lookahead_var.get()))),
        )
        if self.session_status == "complete":
            self.session_path = None
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress_var.set(resume_count / max(1, len(self.rows)) * 100)
        self.eta_var.set("Estimated time remaining: calculating…")
        self.status_var.set(
            f"Resuming after {resume_count} completed rows…" if resume_count else "Starting…"
        )
        self._session_warning_sent = False
        self._persist_session(config, resume_count, "running")
        self.worker = threading.Thread(target=self._process_rows, args=(config,), daemon=True)
        self.worker.start()

    def _stop_processing(self) -> None:
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self.status_var.set("Stopping after active pipeline work finishes…")

    def _classify_row(self, config, row, client):
        name = row_value(row, self.headers, config.mapping.business_name)
        if not name:
            return ClassificationResult("review", "Business name is blank.", "low"), []
        try:
            return client.classify(name, config.target_business, config.model), []
        except Exception as exc:
            return ClassificationResult(
                "review", "Classification failed; sent to review for safety.", "low"
            ), [f"classification: {exc}"]

    def _process_one_row(
        self,
        config: ProcessingConfig,
        index: int,
        row: list[str],
        client: LMStudioClient,
        source_names: dict[str, str],
        result_names: dict[str, str],
        total: int,
        classification_future: Future | None = None,
    ) -> tuple[dict[str, object], str]:
        business_name = row_value(row, self.headers, config.mapping.business_name)
        phone = row_value(row, self.headers, config.mapping.phone)
        street = row_value(row, self.headers, config.mapping.street)
        city_state = row_value(row, self.headers, config.mapping.city_state)
        raw_url_1 = row_value(row, self.headers, config.mapping.url_1)
        raw_url_2 = row_value(row, self.headers, config.mapping.url_2)
        errors: list[str] = []

        self.events.put(
            {
                "kind": "progress",
                "current": index - 1,
                "total": total,
                "message": f"Classifying {index}/{total}: {business_name or '(blank name)'}",
            }
        )
        classification, errors = (
            classification_future.result() if classification_future is not None
            else self._classify_row(config, row, client)
        )

        age = {
            "website_checked": "",
            "founded_year": "",
            "years_in_business": "",
            "age_evidence": "",
            "age_source_url": "",
            "age_confidence": "",
            "age_status": "disabled",
            "error": "",
        }
        page_cache = {}
        def cached_fetch(url):
            if url not in page_cache:
                try:
                    page_cache[url] = getattr(self, "_website_fetcher", fetch_html)(url)
                    page_cache[page_cache[url][1]] = page_cache[url]
                except Exception as exc:
                    page_cache[url] = exc
            cached = page_cache[url]
            if isinstance(cached, Exception):
                raise cached
            return cached
        should_check = config.check_websites and not (
            config.skip_rejected_websites and classification.classification == "rejected"
        )
        if should_check:
            self.events.put(
                {
                    "kind": "progress",
                    "current": index - 1,
                    "total": total,
                    "message": f"Checking websites {index}/{total}: {business_name}",
                }
            )
            try:
                age = find_business_age((raw_url_1, raw_url_2), fetcher=cached_fetch)
            except Exception as exc:
                age = {
                    "website_checked": "",
                    "founded_year": "",
                    "years_in_business": "",
                    "age_evidence": "",
                    "age_source_url": "",
                    "age_confidence": "",
                    "age_status": "error",
                    "error": str(exc),
                }
            if age["error"]:
                errors.append(f"website: {age['error']}")
        elif classification.classification == "rejected":
            age["age_status"] = "skipped_rejected"

        contacts = empty_contacts()
        if config.find_email or config.find_owner:
            if config.skip_rejected_websites and classification.classification == "rejected":
                contacts = empty_contacts("skipped_rejected")
            else:
                self.events.put({"kind": "progress", "current": index - 1, "total": total,
                                 "message": f"Finding website contacts {index}/{total}: {business_name}"})
                try:
                    contacts = research_contacts(
                        prioritize_candidate_urls((raw_url_1, raw_url_2)), cached_fetch,
                        parse_page, _is_directory_url,
                        lambda pages: client.extract_owners(pages, config.model, business_name),
                        config.find_email, config.find_owner, business_name,
                        review_model=lambda contexts: client.review_name_contexts(contexts, config.model),
                    )
                except Exception as exc:
                    contacts = empty_contacts("error")
                    contacts["contact_research_error"] = str(exc)[:1000]

        original: dict[str, object] = {
            header: row[position] if position < len(row) else ""
            for position, header in enumerate(self.headers)
        }
        if source_names:
            source_path, source_row = self.row_sources[index - 1]
            original[source_names["source_file"]] = Path(source_path).name
            original[source_names["source_row"]] = source_row
        values = {
            "normalized_business_name": business_name,
            "normalized_phone": phone,
            "normalized_address": compose_address(street, city_state),
            "normalized_url_1": extract_redirect_target(raw_url_1),
            "normalized_url_2": extract_redirect_target(raw_url_2),
            "classification": classification.classification,
            "classification_reason": classification.reason,
            "classification_confidence": classification.confidence,
            "website_checked": age["website_checked"],
            "founded_year": age["founded_year"],
            "years_in_business": age["years_in_business"],
            "age_evidence": age["age_evidence"],
            "age_source_url": age["age_source_url"],
            "age_confidence": age["age_confidence"],
            "age_status": age["age_status"],
            "processing_error": " | ".join(errors)[:1000],
        }
        if config.find_email or config.find_owner:
            values.update(contacts)
        original.update({result_names[key]: value for key, value in values.items()})
        return original, business_name

    def _process_rows(self, config: ProcessingConfig) -> None:
        client = LMStudioClient(config.server_url)
        self._website_fetcher = PageFetchCache(fetch_html)
        partial_path = partial_output_path(config.output_path)
        Path(partial_path).parent.mkdir(parents=True, exist_ok=True)
        include_source_columns = len(config.input_paths) > 1
        source_names, result_names, output_headers = output_field_layout(
            self.headers, include_source_columns, config.find_email or config.find_owner
        )
        completed = config.resume_count
        self.events.put(
            {
                "kind": "run_started",
                "started_at": time.monotonic(),
                "completed": config.resume_count,
                "total": len(self.rows),
            }
        )
        try:
            mode = "a" if config.resume_count else "w"
            with open(partial_path, mode, newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=output_headers, extrasaction="ignore")
                if not config.resume_count:
                    writer.writeheader()
                total = len(self.rows)
                if config.resume_count:
                    self.events.put(
                        {
                            "kind": "log",
                            "message": f"Resuming at row {config.resume_count + 1} of {total}.",
                        }
                    )
                workers = max(1, min(8, config.pipeline_workers))
                lookahead = max(workers, min(128, config.classification_lookahead))
                self.events.put(
                    {
                        "kind": "log",
                        "message": (
                            f"Classification runs up to {lookahead} rows ahead; "
                            f"{workers} website workers follow. Output stays in source order."
                        ),
                    }
                )
                indexed_rows = iter(
                    enumerate(
                        self.rows[config.resume_count :],
                        start=config.resume_count + 1,
                    )
                )
                pending: list[tuple[int, Future[tuple[dict[str, object], str]]]] = []
                with ThreadPoolExecutor(max_workers=1) as classifier, ThreadPoolExecutor(max_workers=workers) as executor:
                    for _ in range(lookahead):
                        if self.stop_event.is_set():
                            break
                        try:
                            index, row = next(indexed_rows)
                        except StopIteration:
                            break
                        pending.append(
                            (
                                index,
                                executor.submit(
                                    self._process_one_row,
                                    config,
                                    index,
                                    row,
                                    client,
                                    source_names if include_source_columns else {},
                                    result_names,
                                    total,
                                    classifier.submit(self._classify_row, config, row, client),
                                ),
                            )
                        )

                    while pending:
                        index, future = pending.pop(0)
                        original, business_name = future.result()
                        writer.writerow(original)
                        handle.flush()
                        completed = index
                        self._persist_session(config, completed, "running")
                        self.events.put(
                            {
                                "kind": "progress",
                                "current": index,
                                "total": total,
                                "message": f"Completed {index}/{total}: {business_name}",
                                "completed_row": True,
                                "completed_at": time.monotonic(),
                            }
                        )
                        if self.stop_event.is_set():
                            continue
                        try:
                            next_index, next_row = next(indexed_rows)
                        except StopIteration:
                            continue
                        pending.append(
                            (
                                next_index,
                                executor.submit(
                                    self._process_one_row,
                                    config,
                                    next_index,
                                    next_row,
                                    client,
                                    source_names if include_source_columns else {},
                                    result_names,
                                    total,
                                    classifier.submit(self._classify_row, config, next_row, client),
                                ),
                            )
                        )

            if self.stop_event.is_set():
                self._persist_session(config, completed, "stopped")
                self.events.put(
                    {
                        "kind": "complete",
                        "stopped": True,
                        "completed": completed,
                        "path": partial_path,
                    }
                )
            else:
                os.replace(partial_path, config.output_path)
                self._persist_session(config, completed, "complete")
                self.events.put(
                    {
                        "kind": "complete",
                        "stopped": False,
                        "completed": completed,
                        "path": config.output_path,
                    }
                )
        except Exception as exc:
            self._persist_session(config, completed, "failed", str(exc))
            self.events.put(
                {
                    "kind": "error",
                    "message": f"Processing stopped: {exc}\nPartial output: {partial_path}",
                    "context": "processing",
                }
            )

    def _update_eta(self, current: int, total: int, completed_at: float) -> None:
        if self._eta_started_at is None:
            self._eta_started_at = completed_at
            self._eta_started_completed = max(0, current - 1)
            self._eta_samples.clear()
            self._eta_samples.append((completed_at, self._eta_started_completed))
        if self._eta_samples and current <= self._eta_samples[-1][1]:
            return
        self._eta_samples.append((completed_at, current))
        remaining_seconds = estimate_remaining_seconds(
            self._eta_started_at,
            self._eta_started_completed,
            list(self._eta_samples),
            current,
            total,
        )
        duration = format_remaining_time(remaining_seconds)
        if remaining_seconds is None or remaining_seconds <= 0:
            self.eta_var.set(f"Estimated time remaining: {duration}")
            return
        finish = (datetime.now() + timedelta(seconds=remaining_seconds)).strftime("%I:%M %p")
        self.eta_var.set(
            f"Estimated time remaining: {duration}  •  Approx. finish: {finish.lstrip('0')}"
        )

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event.get("kind")
                if kind == "run_started":
                    self._eta_started_at = float(event["started_at"])
                    self._eta_started_completed = int(event["completed"])
                    self._eta_samples.clear()
                    self._eta_samples.append(
                        (self._eta_started_at, self._eta_started_completed)
                    )
                    self.eta_var.set("Estimated time remaining: calculating…")
                elif kind == "models":
                    models = event["models"]
                    self.model_box["values"] = models
                    current = self.model_var.get()
                    preferred = (
                        current
                        if current in models
                        else next(
                            (model for model in models if "gemma-4-e2b" in model.lower()),
                            models[0],
                        )
                    )
                    self.model_var.set(preferred)
                    self.connect_button.configure(state="normal")
                    self.status_var.set(f"Connected. Found {len(models)} chat model(s).")
                    self._write_log("LM Studio models: " + ", ".join(models))
                elif kind == "progress":
                    current = event["current"]
                    total = max(1, event["total"])
                    if event.get("completed_row"):
                        self.progress_var.set(current / total * 100)
                        self._update_eta(
                            current,
                            total,
                            float(event.get("completed_at") or time.monotonic()),
                        )
                    self.status_var.set(event["message"])
                elif kind == "log":
                    self._write_log(event["message"])
                elif kind == "complete":
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.progress_var.set(100 if not event["stopped"] else self.progress_var.get())
                    cleanup_ready = False
                    if event["stopped"]:
                        self.eta_var.set("Estimated time remaining: paused")
                        message = (
                            f"Stopped after {event['completed']} rows. Partial results were saved to:\n"
                            f"{event['path']}"
                        )
                    else:
                        self.eta_var.set("Estimated time remaining: complete")
                        message = f"Completed {event['completed']} rows. Output saved to:\n{event['path']}"
                        cleanup_ready = self._load_dedupe_file(event["path"], select_tab=True)
                        if cleanup_ready:
                            message += (
                                "\n\nThe finished file is ready in the Duplicate Removal tab."
                            )
                    self.status_var.set(message.replace("\n", " "))
                    self._write_log(message)
                    messagebox.showinfo(APP_TITLE, message)
                    if cleanup_ready and self.dedupe_auto_after_run_var.get():
                        self._run_deduplication()
                elif kind == "error":
                    self.eta_var.set("Estimated time remaining: unavailable")
                    self.connect_button.configure(state="normal")
                    if event.get("context") == "processing":
                        self.start_button.configure(state="normal")
                        self.stop_button.configure(state="disabled")
                    self.status_var.set(event["message"].splitlines()[0])
                    self._write_log(event["message"])
                    messagebox.showerror(APP_TITLE, event["message"])
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _write_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{timestamp}] {message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                APP_TITLE, "Processing is still running. Stop it and close the application?"
            ):
                return
            self.stop_event.set()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    BusinessClassifierApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
