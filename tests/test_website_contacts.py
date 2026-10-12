import csv
import queue
import tempfile
import unittest
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import business_classifier_app as app
from website_contacts import published_emails, research_contacts, validate_people


class WebsiteContactTests(unittest.TestCase):
    def research(self, text, response=None, email=True, owner=True, links=''):
        fetch = Mock(return_value=(f'<p>{text}</p>{links}', 'https://example.com'))
        ask = Mock(return_value=response or {'owners': []})
        result = research_contacts(['https://example.com'], fetch, app.parse_page,
                                   app._is_directory_url, ask, email, owner)
        return result, ask

    def test_general_email_without_ai(self):
        result, ask = self.research('Contact info@example.com', owner=False)
        self.assertEqual(result['email'], 'info@example.com')
        self.assertEqual(result['email_type'], 'website')
        ask.assert_not_called()

    def test_owner_and_owner_email(self):
        quote = 'Michael, the owner, can be reached at michael@example.com.'
        result, _ = self.research(quote, {'owners': [{'name': 'Michael', 'page': 0,
            'evidence': quote, 'email': 'michael@example.com', 'email_evidence': quote}]})
        self.assertEqual(result['owner_name'], 'Michael')
        self.assertEqual(result['email_type'], 'owner_explicit_mention')

    def test_unrelated_person_and_owner_not_associated(self):
        quote = 'Andrew got us a great price and the owner checked everything.'
        result, _ = self.research(quote, {'owners': [{'name': 'Andrew', 'page': 0, 'evidence': quote}]})
        self.assertEqual(result['owner_name'], '')

    def test_explicit_owner_contact_sentence_survives_model_email_omission(self):
        quote = 'Michael, the owner, can be reached at michael@example.com.'
        result, _ = self.research(quote, {'owners': [{'name': 'Michael', 'page': 0, 'evidence': quote}]})
        self.assertEqual(result['email_type'], 'owner_explicit_mention')
        self.assertEqual(result['email'], 'michael@example.com')

    def test_founder_is_not_owner(self):
        quote = 'Our founder Sarah started the company.'
        self.assertEqual(validate_people({'owners': [{'name': 'Sarah', 'page': 0, 'evidence': quote}]},
                         [{'url': 'https://example.com', 'text': quote, 'emails': []}]), [])

    def test_invented_evidence_and_email_rejected(self):
        result, _ = self.research('Michael is the owner. Contact info@example.com.',
            {'owners': [{'name': 'Michael', 'page': 0, 'evidence': 'Michael is the owner.',
                        'email': 'michael@example.com', 'email_evidence': 'Michael owns michael@example.com'}]})
        self.assertEqual(result['email'], 'info@example.com')
        self.assertEqual(result['email_type'], 'website')
        result, _ = self.research('The owner runs our business.',
            {'owners': [{'name': 'Madeup', 'page': 0, 'evidence': 'Madeup is the owner.'}]})
        self.assertEqual(result['owner_name'], '')

    def test_mailto_and_entity(self):
        result, _ = self.research('Contact us', links='<a href="mailto:hello%40example.com?subject=Hi">Email</a>')
        self.assertEqual(result['email'], 'hello@example.com')
        self.assertEqual(published_emails('logo@2x.png', []), [])

    def test_external_links_ignored_and_bounded(self):
        fetch = Mock(return_value=('<a href="https://elsewhere.test/about">About</a>' +
             ''.join(f'<a href="/about/{n}">Team</a>' for n in range(20)), 'https://example.com'))
        research_contacts(['https://example.com'], fetch, app.parse_page, app._is_directory_url,
                          Mock(), True, True)
        self.assertEqual(fetch.call_count, 4)
        self.assertTrue(all('elsewhere' not in call.args[0] for call in fetch.call_args_list))

    def test_directory_only_does_not_fetch(self):
        fetch = Mock()
        result = research_contacts(['https://manta.com/c/foo'], fetch, app.parse_page,
                                   app._is_directory_url, Mock(), True, True)
        self.assertEqual(result['contact_research_status'], 'no_business_website')
        fetch.assert_not_called()

    def test_failure_does_not_discard_email(self):
        result = research_contacts(['https://example.com'],
            lambda url: ('Michael is the owner. info@example.com', url), app.parse_page,
            app._is_directory_url, Mock(side_effect=ValueError('bad response')), True, True)
        self.assertEqual(result['email'], 'info@example.com')
        self.assertEqual(result['contact_research_status'], 'partial_error')

    def test_owner_only_has_no_email(self):
        result, _ = self.research('Michael is the owner. info@example.com',
            {'owners': [{'name': 'Michael', 'page': 0, 'evidence': 'Michael is the owner.'}]}, email=False)
        self.assertEqual(result['owner_name'], 'Michael')
        self.assertEqual(result['email'], '')

    def test_legacy_output_layout_unchanged(self):
        _, names, fields = app.output_field_layout(['Name'], False)
        self.assertNotIn('email', names)
        self.assertEqual(fields, ['Name', *app.RESULT_FIELDS])

    def test_new_output_resume_and_collision(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'partial.csv'
            headers, rows = ['Name', 'email'], [['Acme', 'original']]
            _, names, fields = app.output_field_layout(headers, False, True)
            self.assertEqual(names['email'], 'email_2')
            with path.open('w', newline='', encoding='utf-8-sig') as handle:
                writer = csv.DictWriter(handle, fields)
                writer.writeheader()
                writer.writerow({'Name': 'Acme', 'email': 'original', 'email_2': 'info@example.com'})
            self.assertEqual(app.compatible_partial_row_count(str(path), headers, rows, include_contacts=True), 1)
            self.assertEqual(app.compatible_partial_row_count(str(path), headers, rows), 0)

    def test_worker_reuses_age_fetch_and_can_run_without_age(self):
        instance = app.BusinessClassifierApp.__new__(app.BusinessClassifierApp)
        instance.headers = ['Name', 'URL']
        instance.events = queue.Queue()
        config = app.ProcessingConfig(('in.csv',), 'out.csv', 'Concrete', 'http://localhost:1234',
            'test', app.MappingConfig('Name', url_1='URL'), True, True, find_email=True)
        _, names, _ = app.output_field_layout(instance.headers, False, True)
        future = Future()
        future.set_result((app.ClassificationResult('approved', 'test', 'high'), []))
        for age_enabled in (True, False):
            with patch.object(app, 'fetch_html', return_value=('Established in 1999. info@example.com',
                                                                'https://example.com')) as fetch:
                row, _ = instance._process_one_row(replace(config, check_websites=age_enabled), 1,
                    ['Acme', 'https://example.com'], Mock(), {}, names, 1, future)
                self.assertEqual(row['email'], 'info@example.com')
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(row['age_status'], 'found' if age_enabled else 'disabled')


if __name__ == '__main__':
    unittest.main()
