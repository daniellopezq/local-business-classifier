import csv
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import business_classifier_app as mod


class PipelineTests(unittest.TestCase):
    def test_classification_runs_ahead_and_stop_drains_ordered_prefix(self):
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
                self.assertEqual([r['Title'] for r in csv.DictReader(handle)],
                                 [str(i) for i in range(8)])
            self.assertEqual(len(classified), 8)
            self.assertEqual(mod.compatible_partial_row_count(
                partial, app.headers, app.rows), 8)

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
