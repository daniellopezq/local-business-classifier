# Optional website contact research

In Local Business Classifier, enable either or both options before starting:

- **Find website email (owner preferred)**: extract a published address from visible page text or a mailto link. Prefer an explicitly associated owner's email; otherwise return a website email. Never guess an address or send mail.
- **Find owner/contact names (includes unconfirmed)**: keep explicit ownership statements and possible contacts mentioned in website text/testimonials. Also suggest names from person-like public email usernames. Founder, manager, and president alone do not establish ownership.

Both options default to off, work independently of the age checkbox, and follow the existing skip-rejected setting. Reload/restart the app after updating it. Review column previews, select the real website URL columns, and use a new output filename when enabling research on an old run. For a small trial, load a separate small input file.

The existing website workers perform research after classification. Research attempts at most four pages per row, including relevant same-site About, Contact, Team, and testimonial/review links. Pages fetched for age are reused within the row. Directory-only URLs are marked `no_business_website`; the app does not search Google for a replacement site. JavaScript widgets, iframe reviews, login-protected content and obfuscated email addresses are not supported. Not found does not mean no owner/email exists.

Classification and extraction share the selected model through a serialized inference lock; no second model is loaded. Public page excerpts are sent to the configured LM Studio server. Keep that server local if local-only processing is required. Enabling extraction adds work and may reduce classification throughput.

## CSV fields

When either option is enabled, the CSV adds:

- `owner_name`, `owner_status`, `owner_evidence`, `owner_source_url`
- `email`, `email_type`, `email_evidence`, `email_source_url`
- `contact_research_status`, `contact_research_error`
- `contact_name`, `contact_status`, `contact_evidence`, `contact_source_url`, `contact_candidates`

`contact_name` retains possible business contacts, with `contact_status=to_be_confirmed`. A name such as Jon can come from testimonial text or the public address `jon@plumberjons.com`, without becoming a verified owner. Email usernames are guesses: generic inboxes such as info/support are ignored; merged names are not split into invented first/last names. `contact_candidates` is a JSON list preserving each candidate's name, source, evidence, status and role. Clear reviewers/customers or owners of another named business remain in that list with exclusion labels, not in the primary business-contact/owner columns. Ambiguous mentions remain available for review. Exact website quotes are still required for model-suggested names; guesses must not be fabricated without a source.

The expanded contact columns require a **new output filename** for runs started before this update. Older partials are left untouched and are not silently appended with a different header. Old results are not retroactively enriched.

### Context review of names

Email usernames containing multiple distinctive words from the business name are retained in the candidate audit as `business_email_not_person`, but excluded from the primary contact-name fields. The published email is still exported. This rule also works when model review is unavailable. An unknown email candidate whose model explanation identifies a generic business inbox or no identifiable person is similarly excluded. A single shared word is not enough for the business-name rule, so a plausible personal name such as Jon is preserved. These checks reuse existing context review without extra page downloads or model calls.

For email-derived candidates, context review may suggest spacing and capitalization of a merged username: for example, `bradypease510@example.com` may suggest `Brady Pease`. The suggestion must preserve every letter of the original candidate in order. It remains `to_be_confirmed`, never establishes ownership, and retains the original name and email evidence in `contact_candidates` with `name_inference=email_username_model_guess`. If the model is unavailable or cannot suggest a valid interpretation, the original candidate remains available for review. Previously completed rows are not rewritten.

After extraction, candidates (including email-derived guesses) go through a separate call to the selected local model. The call includes the target business, source URL, evidence and surrounding page text. It distinguishes people from organizations, and target-business contacts from reviewers/customers or people associated with another business. Copyright-footer business names are not human contacts. Plausible but uncertain human names remain `to_be_confirmed`; they are not upgraded to verified identities.

The existing `contact_candidates` JSON now records `context_review`, `entity_type`, `relationship` and `context_reason`. Excluded candidates remain there for audit but are removed from the primary name fields. An explicit owner must also pass the target-business/person check. If review fails, candidate names remain with `to_be_confirmed_context_unchecked` and a diagnostic, while ownership is not asserted. This uses the same CSV columns as the preceding contact-name version.

Reviews run in batches of four candidates, capped at 24 per row to bound model work. Remaining candidates are labeled unchecked for manual review. This adds inference time, not additional website requests. The selected model and existing inference lock are shared with classification. Restart the app to use this version. Existing saved rows are not rewritten; use a new output to reprocess them with contextual review.

`explicit_mention` is a website claim, not independently verified ownership. Multiple names are retained and flagged for review. Emails are published addresses, not verified deliverable mailboxes. Model quotes must match fetched text; names and ownership wording must match the quote, and owner email evidence must include the address, name and ownership role. Keep evidence for manual review.

Existing source columns are preserved; conflicting result column names receive suffixes. With both options disabled, the old CSV layout remains unchanged. Research results checkpoint with each completed row, using existing partial CSV/session recovery. Interrupted in-flight rows may be repeated. New option values are saved in sessions and respected by `resume_latest_session.py`. Changed research options require a new output or restoring original options before resume.

## Age results and efficient page fetching

The existing `age_status` column now distinguishes:

- `found`: an age statement was found, with evidence and source URL.
- `not_found`: pages loaded but no supported age statement was found.
- `website_blocked`: the first preferred source refused or rate-limited access (HTTP 401, 403 or 429), and no alternative source loaded.
- `website_unavailable`: the first preferred source failed (such as DNS, timeout or missing page), and no alternative source loaded.
- `age_check_incomplete`: a page loaded, but further age research failed, such as a blocked About page. Inspect `processing_error` for the specific failed URL.
- `no_url`, `disabled`, and `skipped_rejected` retain their existing meanings. Unexpected row-level failures may still report `error`.

The app unwraps directory redirect URLs locally and checks direct business URLs first. Once a business website loads, it no longer queries Manta merely because an age statement was absent; this saves requests but may miss an age published only on the directory. If the business website cannot load, the supplied directory remains a fallback. A successful alternate lookup is not reported as a total failure.

Age and contact workers reuse pages through a small, in-memory per-run cache (eight URL entries), including canonical redirect URLs. Concurrent requests for an identical URL share the same fetch. Failed downloads are not retained across rows. The cache is reset for the next run, does not alter saved CSV/session formats, and never bypasses access restrictions. Existing saved rows are not automatically rechecked or relabeled.

## Google reviews

Not implemented. Places API returns at most five reviews. Business Profile paginated reviews require authorized access to managed business locations. Google Maps terms restrict copying/exporting reviews and creating derived content, so an API key alone is not approval for a review-to-lead CSV workflow. Source permissions and product fit must be resolved before adding this integration.
