import csv
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import business_classifier_app as mod


class PipelineTests(unittest.TestCase):
    def test_classification_runs_ahead_and_stop_saves_only_completed_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            app = mod.BusinessClassifierApp.__new__(mod.BusinessClassifierApp)
            app.headers = ['Title']
            app.rows = [[str(i)] for i in range(20)]
            app.row_sources = []
            app.events = queue.Queue()
            app.stop_event = threading.Event()
            app._persist_session = lambda *args: None
            classified = []
            ready = threading.Event()

            def classify(config, row, client):
                classified.append(row[0])
                if len(classified) == 8:
                    app.stop_event.set()
                    ready.set()
                return mod.ClassificationResult('approved', 'test', 'high'), []

            def websites(config, index, row, client, sources, names, total, future):
                future.result()
                self.assertTrue(ready.wait(5), 'GPU stage stalled behind websites')
                app._check_stopped()
                return {'Title': row[0]}, row[0]

            app._classify_row = classify
            app._process_one_row = websites
            config = mod.ProcessingConfig(
                input_paths=('input.csv',), output_path=str(Path(folder) / 'out.csv'),
                target_business='Contractor', server_url='http://localhost:1234',
                model='test', mapping=mod.MappingConfig(business_name='Title'),
                check_websites=True, skip_rejected_websites=True,
                pipeline_workers=2, classification_lookahead=8,
            )
            app._process_rows(config)
            partial = mod.partial_output_path(config.output_path)
            with open(partial, encoding='utf-8-sig', newline='') as handle:
                saved = [r['Title'] for r in csv.DictReader(handle)]
                self.assertEqual(saved, [str(i) for i in range(len(saved))])
                self.assertLessEqual(len(saved), 2, 'Queued website jobs were drained after stop')
            self.assertEqual(len(classified), 8)
            self.assertEqual(mod.compatible_partial_row_count(
                partial, app.headers, app.rows), len(saved))

    def test_stop_during_active_inference_cancels_backlog_and_saves_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            app = mod.BusinessClassifierApp.__new__(mod.BusinessClassifierApp)
            app.headers, app.rows, app.row_sources = ['Title'], [[str(i)] for i in range(100)], []
            app.events, app.stop_event = queue.Queue(), threading.Event()
            app._persist_session = Mock()
            entered, release = threading.Event(), threading.Event()
            def inference(*args):
                entered.set()
                self.assertTrue(release.wait(5))
                return mod.ClassificationResult('approved', 'matched', 'high')
            config = mod.ProcessingConfig(('input.csv',), str(Path(folder) / 'out.csv'),
                'Contractor', 'http://localhost:1234', 'test', mod.MappingConfig('Title'),
                False, True, pipeline_workers=4, classification_lookahead=64)
            with patch.object(mod.LMStudioClient, 'classify', side_effect=inference) as classify:
                worker = threading.Thread(target=app._process_rows, args=(config,), daemon=True)
                worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    app.stop_event.set()
                finally:
                    release.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(classify.call_count, 1)
            app._persist_session.assert_called_with(config, 0, 'stopped')
            self.assertEqual(mod.compatible_partial_row_count(
                mod.partial_output_path(config.output_path), app.headers, app.rows), 0)
            self.assertFalse(Path(config.output_path).exists())
            events = list(app.events.queue)
            self.assertFalse(any(e['kind'] == 'error' for e in events))
            self.assertTrue(events[-1]['stopped'])

    def test_model_waiting_for_lock_does_not_submit_after_stop(self):
        client = mod.LMStudioClient('http://localhost:1234')
        client.stop_event = threading.Event()
        with client._inference_lock:
            with mod.ThreadPoolExecutor(max_workers=1) as pool:
                # Set cancellation before releasing the inference lock.
                client.stop_event.set()
                with patch.object(mod, '_request_json') as request:
                    future = pool.submit(client._contact_json, [], 'test')
                    client._inference_lock.release()
                    try:
                        with self.assertRaises(mod.CancelledError):
                            future.result(timeout=5)
                        request.assert_not_called()
                    finally:
                        client._inference_lock.acquire()

    def test_stop_during_page_fetch_does_not_follow_links_or_extract_contacts(self):
        app = mod.BusinessClassifierApp.__new__(mod.BusinessClassifierApp)
        app.headers = ['Title', 'URL']
        app.events, app.stop_event = queue.Queue(), threading.Event()
        config = mod.ProcessingConfig(('input.csv',), 'out.csv', 'Contractor',
            'http://localhost:1234', 'test', mod.MappingConfig(business_name='Title', url_1='URL'),
            True, True, find_email=True, find_owner=True)
        future = mod.Future()
        future.set_result((mod.ClassificationResult('approved', 'matched', 'high'), []))
        client = Mock()
        def fetch(url):
            app.stop_event.set()
            return '<a href="/about">About</a><p>Contact jon@example.com</p>', url
        with patch.object(mod, 'fetch_html', side_effect=fetch) as fetcher:
            with self.assertRaises(mod.CancelledError):
                app._process_one_row(config, 1, ['Example', 'https://example.com'], client,
                    {}, mod.unique_result_field_names(app.headers), 1, future)
        self.assertEqual(fetcher.call_count, 1)
        client.extract_owners.assert_not_called()

    def test_close_keeps_window_until_worker_has_saved_and_exited(self):
        app = mod.BusinessClassifierApp.__new__(mod.BusinessClassifierApp)
        app.root, app.worker = Mock(), Mock()
        app.start_button, app.stop_button, app.status_var = Mock(), Mock(), Mock()
        app.stop_event = threading.Event()
        app.worker.is_alive.return_value = True
        with patch.object(mod.messagebox, 'askyesno', return_value=True):
            app._on_close()
        self.assertTrue(app.stop_event.is_set())
        app.root.destroy.assert_not_called()
        app.root.after.assert_called_once_with(100, app._finish_close)
        app.worker.is_alive.return_value = False
        app._finish_close()
        app.root.destroy.assert_called_once()

    def test_actual_row_consumes_preclassified_result_without_second_model_call(self):
        from concurrent.futures import Future
        app = mod.BusinessClassifierApp.__new__(mod.BusinessClassifierApp)
        app.headers = ['Title']
        app.events = queue.Queue()
        future = Future()
        future.set_result((mod.ClassificationResult('approved', 'matched', 'high'), []))
        config = mod.ProcessingConfig(
            ('input.csv',), 'out.csv', 'Contractor', 'http://localhost:1234',
            'test', mod.MappingConfig('Title'), False, True,
        )
        names = mod.unique_result_field_names(app.headers)
        with patch.object(mod.LMStudioClient, 'classify', side_effect=AssertionError('duplicate inference')):
            record, name = app._process_one_row(config, 1, ['Acme'],
                mod.LMStudioClient(config.server_url), {}, names, 1, future)
        self.assertEqual(record['classification'], 'approved')
        self.assertEqual(name, 'Acme')


if __name__ == '__main__':
    unittest.main()
