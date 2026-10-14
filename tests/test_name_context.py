import json
import unittest
from unittest.mock import Mock

import business_classifier_app as app
from website_contacts import research_contacts, review_candidate_contexts


class NameContextTests(unittest.TestCase):
    def run_research(self, name, quote, entity, relationship, owners=False):
        payload = {'owners': [], 'contacts': []}
        payload['owners' if owners else 'contacts'] = [{'name': name, 'page': 0, 'evidence': quote}]
        reviewer = Mock(return_value={'decisions': [{'id': 0, 'entity_type': entity,
                    'relationship': relationship, 'reason': 'Reviewed surrounding text'}]})
        result = research_contacts(['https://example.com'], lambda url: (quote, url),
            app.parse_page, app._is_directory_url, Mock(return_value=payload), True, True,
            'Peak Cleaning', reviewer)
        return result, reviewer

    def test_copyright_organizations_excluded(self):
        for name in ('Pure End of Tenancy Cleaning', 'Vanguard Cleaning Systems of the Southern Valley'):
            with self.subTest(name=name):
                quote = f'Copyright 2026 {name}. All rights reserved.'
                result, reviewer = self.run_research(name, quote, 'organization', 'target_business')
                self.assertEqual(result['contact_name'], '')
                self.assertEqual(result['contact_research_status'], 'not_found')
                self.assertIn('not_a_person', result['contact_candidates'])
                self.assertIn(quote, reviewer.call_args.args[0][0]['context'])

    def test_natalie_owner_kept(self):
        result, _ = self.run_research('Natalie Cargin', 'Natalie Cargin, Owner & Founder',
                                      'person', 'target_business', owners=True)
        self.assertEqual(result['owner_name'], 'Natalie Cargin')
        self.assertEqual(result['contact_name'], 'Natalie Cargin')

    def test_uncertain_person_kept(self):
        result, _ = self.run_research('Jon', 'Jon did a great job.', 'person', 'unknown')
        self.assertEqual(result['contact_name'], 'Jon')
        self.assertEqual(result['contact_status'], 'to_be_confirmed')

    def test_reviewer_excluded(self):
        result, _ = self.run_research('Tammy', 'Tammy: I recommend this company.', 'person', 'customer')
        self.assertEqual(result['contact_name'], '')
        self.assertIn('review_author_not_target', result['contact_candidates'])

    def test_wrong_owner_demoted_by_context(self):
        result, _ = self.run_research('James Chen', 'James Chen, Owner', 'person', 'other_business', owners=True)
        self.assertEqual(result['owner_name'], '')
        self.assertEqual(result['contact_name'], '')

    def test_email_candidate_is_reviewed(self):
        reviewer = Mock(return_value={'decisions': [{'id': 0, 'entity_type': 'person',
                    'relationship': 'unknown', 'reason': 'Email-derived possible first name'}]})
        result = research_contacts(['https://example.com'], lambda url: ('Email jon@example.com', url),
            app.parse_page, app._is_directory_url, lambda pages: {'owners': [], 'contacts': []},
            True, True, 'Plumber Jon', reviewer)
        self.assertEqual(result['contact_name'], 'Jon')
        self.assertEqual(reviewer.call_args.args[0][0]['source'], 'email')

    def test_invalid_or_failed_review_retains_unchecked_candidate(self):
        for reviewer in (Mock(side_effect=TimeoutError('offline')), Mock(return_value={'decisions': []}),
                         Mock(return_value={'decisions': [{'id': 90}]})):
            result = research_contacts(['https://example.com'], lambda url: ('Email jon@example.com', url),
                app.parse_page, app._is_directory_url, lambda pages: {'owners': [], 'contacts': []},
                True, True, 'Plumber Jon', reviewer)
            self.assertEqual(result['contact_name'], 'Jon')
            self.assertEqual(result['contact_status'], 'to_be_confirmed_context_unchecked')
            self.assertEqual(result['contact_research_status'], 'partial_error')

    def test_batched_ids_and_context_bound(self):
        people = [{'name': f'Name{i}', 'evidence': f'Name{i}', 'source': 'website_text',
                   'url': 'https://example.com', 'status': 'to_confirm_website_mention'} for i in range(6)]
        pages = [{'url': 'https://example.com', 'text': ' '.join(p['name'] for p in people)}]
        reviewer = Mock(side_effect=lambda batch: {'decisions': [{'id': p['id'], 'entity_type': 'person',
                       'relationship': 'unknown'} for p in batch]})
        reviewed, errors = review_candidate_contexts(people, pages, 'Acme', reviewer)
        self.assertFalse(errors)
        self.assertEqual(reviewer.call_count, 2)
        self.assertTrue(all(p['context_review'] == 'checked' for p in reviewed))

    def test_merged_email_name_can_be_split_but_never_asserts_owner(self):
        reviewer = Mock(return_value={'decisions': [{'id': 0, 'entity_type': 'person',
            'relationship': 'unknown', 'suggested_name': 'Brady Pease'}]})
        result = research_contacts(['https://example.com'],
            lambda url: ('Contact Bradypease510@example.com', url), app.parse_page,
            app._is_directory_url, lambda pages: {'owners': [], 'contacts': []},
            True, True, 'Example Exterior Cleaning', reviewer)
        self.assertEqual(result['contact_name'], 'Brady Pease')
        self.assertEqual(result['contact_status'], 'to_be_confirmed')
        self.assertEqual(result['owner_name'], '')
        candidate = json.loads(result['contact_candidates'])[0]
        self.assertEqual(candidate['evidence'], 'Bradypease510@example.com')
        self.assertEqual(candidate['original_name'], 'Bradypease')
        self.assertEqual(candidate['name_inference'], 'email_username_model_guess')

    def test_model_cannot_invent_letters_or_rename_website_names(self):
        for source, proposed in [('email', 'Bradley Pease'), ('website_text', 'Brady Pease')]:
            people = [{'name': 'Bradypease', 'source': source, 'evidence': 'Bradypease',
                       'url': 'https://example.com', 'status': 'to_confirm_email_guess'}]
            reviewer = Mock(return_value={'decisions': [{'id': 0, 'entity_type': 'person',
                'relationship': 'unknown', 'suggested_name': proposed}]})
            reviewed, errors = review_candidate_contexts(people, [], 'Example', reviewer)
            self.assertFalse(errors)
            self.assertEqual(reviewed[0]['name'], 'Bradypease')


if __name__ == '__main__':
    unittest.main()
