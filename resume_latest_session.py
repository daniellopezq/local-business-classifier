"""Open the GUI and automatically resume the newest unfinished session."""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from business_classifier_app import (
    NONE_COLUMN,
    BusinessClassifierApp,
    BusinessClassifierError,
    LMStudioClient,
    MappingConfig,
    ProcessingConfig,
    compatible_partial_row_count,
    list_session_states,
    partial_output_path,
    read_tabular_files,
)


def main() -> None:
    session = next(
        (
            item
            for item in list_session_states()
            if item[1].get("status") != "complete"
        ),
        None,
    )
    if session is None:
        raise BusinessClassifierError("No unfinished classifier session was found.")
    session_path, payload = session

    input_paths = tuple(str(path) for path in payload.get("input_paths") or ())
    output_path = str(payload.get("output_path") or "")
    headers, rows, row_sources = read_tabular_files(input_paths)
    include_source_columns = len(input_paths) > 1
    resume_count = compatible_partial_row_count(
        partial_output_path(output_path),
        headers,
        rows,
        row_sources=row_sources,
        include_source_columns=include_source_columns,
        include_contacts=bool(payload.get("find_email") or payload.get("find_owner")),
    )
    if resume_count <= 0 or resume_count >= len(rows):
        raise BusinessClassifierError(
            f"Expected an unfinished compatible partial; found {resume_count:,}/{len(rows):,}."
        )

    server_url = str(payload.get("server_url") or "http://localhost:1234")
    model = str(payload.get("model") or "")
    models = LMStudioClient(server_url).list_models()
    if model not in models:
        raise BusinessClassifierError(f"Saved LM Studio model is unavailable: {model}")

    saved_mapping = payload.get("mapping") or {}
    mapping = MappingConfig(
        business_name=str(saved_mapping.get("business_name") or ""),
        phone=str(saved_mapping.get("phone") or ""),
        street=str(saved_mapping.get("street") or ""),
        city_state=str(saved_mapping.get("city_state") or ""),
        url_1=str(saved_mapping.get("url_1") or ""),
        url_2=str(saved_mapping.get("url_2") or ""),
    )
    config = ProcessingConfig(
        input_paths=input_paths,
        output_path=output_path,
        target_business=str(payload.get("target_business") or "Contractor"),
        server_url=server_url,
        model=model,
        mapping=mapping,
        check_websites=bool(payload.get("check_websites", True)),
        find_email=bool(payload.get("find_email", False)),
        find_owner=bool(payload.get("find_owner", False)),
        skip_rejected_websites=bool(payload.get("skip_rejected_websites", True)),
        resume_count=resume_count,
        pipeline_workers=max(1, min(8, int(payload.get("pipeline_workers") or 4))),
        classification_lookahead=max(1, min(128, int(payload.get("classification_lookahead") or 64))),
    )

    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    app = BusinessClassifierApp(root, offer_session_restore=False)
    app.input_paths = input_paths
    app.headers = headers
    app.rows = rows
    app.row_sources = row_sources
    app.session_path = session_path
    app.session_status = str(payload.get("status") or "stopped")
    app.input_var.set(
        input_paths[0]
        if len(input_paths) == 1
        else f"{len(input_paths)} files: {Path(input_paths[0]).name} (+{len(input_paths) - 1} more)"
    )
    app.output_var.set(output_path)
    app.target_var.set(config.target_business)
    app.server_var.set(server_url)
    app.model_box["values"] = models
    app.model_var.set(model)
    app.check_websites_var.set(config.check_websites)
    app.find_email_var.set(config.find_email)
    app.find_owner_var.set(config.find_owner)
    app.skip_rejected_var.set(config.skip_rejected_websites)
    app.pipeline_workers_var.set(config.pipeline_workers)
    app.classification_lookahead_var.set(config.classification_lookahead)
    choices = [NONE_COLUMN, *headers]
    for key, value in mapping.__dict__.items():
        app.mapping_boxes[key]["values"] = choices
        app.mapping_vars[key].set(value or NONE_COLUMN)

    app.stop_event.clear()
    app.start_button.configure(state="disabled")
    app.stop_button.configure(state="normal")
    app.progress_var.set(resume_count / len(rows) * 100)
    app.status_var.set(
        f"Resuming at row {resume_count + 1:,} of {len(rows):,}; "
        f"{config.pipeline_workers} website workers, {config.classification_lookahead} rows ahead."
    )
    app._write_log(
        f"Automatically resumed {session_path.name} from {resume_count:,}/{len(rows):,}."
    )
    app._write_log("Classification queue enabled; website workers follow independently.")
    app._persist_session(config, resume_count, "running")
    app.worker = threading.Thread(target=app._process_rows, args=(config,), daemon=True)
    app.worker.start()
    root.mainloop()


if __name__ == "__main__":
    main()
