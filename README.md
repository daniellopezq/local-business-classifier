# Local Business Classifier

A Python/Tkinter desktop app for classifying business lists with a local LM Studio model and optionally researching business websites. Results are saved as CSV.

## Features

- Load CSV or XLSX files; combine multiple files with matching headers.
- Map the actual file columns with population counts and example values.
- Classify names as approved, review, or rejected for a configurable business category.
- Optionally find years-in-business statements, published emails, and possible owner/contact names with source evidence.
- Review candidate names in context using the selected model; uncertain names remain labeled for confirmation.
- Run website workers behind a classification lookahead queue, with progress and estimated time remaining.
- Save partial output and retain the latest three session records for recovery.
- Remove duplicates in separate name, address, and phone passes, with optional toll-free number removal.

## Windows setup

Install Python 3.10 or newer with Tkinter (included in the standard Windows installer), Git, and LM Studio. Download a model in LM Studio and start its local server. Model files are not included.

In PowerShell:

```powershell
git clone https://github.com/daniellopezq/local-business-classifier.git
cd local-business-classifier
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe business_classifier_app.py
```

The only third-party Python dependency is `openpyxl`, for XLSX input. CSV processing uses the standard library. Other operating systems require Python with Tk support; Windows is the primary tested platform.

## Using the app

1. Start the LM Studio local server (default `http://localhost:1234`).
2. Open the app, click **Connect / Refresh**, and select your model.
3. Choose input files and an output filename. Files combined in one run must have matching headers in the same order.
4. Verify every column mapping using the previews. Set your target category, for example `Contractor`.
5. Enable website age, email, or owner/contact research as needed. Check whether rejected businesses should be skipped.
6. Start with a small sample and inspect the output before processing a large list.

Website workers overlap network requests; model inference is serialized. Increasing worker counts does not guarantee better speed. Contact extraction and context review add inference work.

## Recovery

Completed rows are flushed to a partial CSV. Session metadata is stored outside the project under `%LOCALAPPDATA%\LocalBusinessClassifier\sessions` on Windows, retaining three session records. Restore a session through the app, keeping the input files, row order, output path, mapping, and research options unchanged.

The optional helper `python resume_latest_session.py` automatically starts the newest unfinished compatible session. Use it only when you intend to resume immediately. Session files reference your data; they are not backups of the original input files. Back up inputs and outputs separately. Older incompatible output schemas require a new output filename.

## Research limitations and privacy

Classification and contact names can be wrong. Possible names inferred from emails or testimonials are not verified owners. Always inspect the evidence and confirmation labels before using results.

Website research reads fetched HTML, not a browser-rendered page. JavaScript review widgets, iframe reviews, login-protected content, and Google Reviews API integration are not supported. Contact research attempts at most four pages per business. Not found does not mean the information does not exist.

Business names and selected website excerpts are sent to the configured LM Studio server; keep it local for local inference. Website lookup itself makes outbound internet requests. Respect website terms, access restrictions, and applicable privacy requirements. The app does not send emails or verify mailbox deliverability.

See [website contact research details](WEBSITE_CONTACT_RESEARCH.md) for output fields, status meanings, caching, and context review behavior.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use mocks and do not require a running model. They do not establish live-model accuracy or website availability.

## Repository scope

This standalone repository includes only application code, documentation, and tests. Business lists, output CSVs, credentials, model files, and saved sessions are excluded.

## License

Licensed under the [MIT License](LICENSE).
