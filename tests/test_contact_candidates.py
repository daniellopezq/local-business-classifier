import json
import unittest
from unittest.mock import Mock

import business_classifier_app as app
from website_contacts import research_contacts, validate_people, email_name_candidate


class CandidateTests(unittest.TestCase):
    def research(self, text, payload=None, business="Plumber Jon's", find_email=True):
        ask = Mock(return_value=payload or {'owners': [], 'contacts': []})
        result = research_contacts(['https://example.com'], lambda url: (text, url),
            app.parse_page, app._is_directory_url, ask, find_email, True, business)
        return result, ask

    def test_email_name_is_saved_without_owner_claim(self):
        result, _ = self.research('Email jon@plumberjons.com')
        self.assertEqual(result['contact_name'], 'Jon')
        self.assertEqual(result['contact_status'], 'to_be_confirmed')
        self.assertEqual(result['owner_name'], '')
        self.assertEqual(result['email_type'], 'website')
        self.assertIn('to_confirm_email_guess', result['contact_candidates'])

    def test_generic_email_usernames_ignored(self):
        for email in ('info@example.com', 'support@example.com', 'hello@example.com',
                      'sales123@example.com', 'crystalclearcleaning@gmail.com'):
            self.assertEqual(email_name_candidate(email), '')
        self.assertEqual(email_name_candidate('jane.doe22@example.com'), 'Jane Doe')
        self.assertEqual(email_name_candidate('bradypease510@example.com'), 'Bradypease')

    def test_testimonial_person_retained_without_owner_word(self):
        quote = 'Andrew made sure we got the best price.'
        result, ask = self.research('Testimonials: ' + quote,
            {'owners': [], 'contacts': [{'name': 'Andrew', 'page': 0, 'role': 'staff', 'evidence': quote}]})
        self.assertEqual(result['contact_name'], 'Andrew')
        self.assertEqual(result['owner_name'], '')
        self.assertEqual(result['contact_status'], 'to_be_confirmed')
        self.assertIn('Andrew', ask.call_args.args[0][0]['text'])

    def test_review_author_retained_but_not_primary_business_contact(self):
        text = 'Tammy Katuin, Customer. Jon did an excellent job.'
        result, _ = self.research(text, {'owners': [], 'contacts': [
            {'name': 'Tammy Katuin', 'page': 0, 'role': 'customer', 'evidence': 'Tammy Katuin, Customer.'},
            {'name': 'Jon', 'page': 0, 'role': 'staff', 'evidence': 'Jon did an excellent job.'}]})
        self.assertEqual(result['contact_name'], 'Jon')
        self.assertIn('Tammy Katuin', result['contact_candidates'])
        self.assertIn('review_author_not_target', result['contact_candidates'])

    def test_owner_of_other_business_not_claimed_as_owner(self):
        text = 'James Chen, Owner of Golden Dragon Restaurant'
        payload = {'owners': [{'name': 'James Chen', 'page': 0, 'business': 'Golden Dragon Restaurant',
                              'evidence': text}]}
        result, _ = self.research(text, payload, business='Drain Cleaning Fresno')
        self.assertEqual(result['owner_name'], '')
        self.assertEqual(result['contact_name'], '')
        self.assertIn('James Chen', result['contact_candidates'])
        self.assertIn('other_business_not_target', result['contact_candidates'])

    def test_target_owner_still_retained(self):
        text = 'Jon, owner of Plumber Jons Plumbing'
        pages = [{'url': 'https://example.com', 'text': text, 'emails': []}]
        payload = {'owners': [{'name': 'Jon', 'page': 0, 'evidence': text}]}
        self.assertEqual(validate_people(payload, pages, 'Plumber Jons Plumbing LLC')[0]['name'], 'Jon')

    def test_hallucinated_name_not_retained(self):
        result, _ = self.research('We provide plumbing services.',
            {'owners': [], 'contacts': [{'name': 'Invented', 'page': 0, 'evidence': 'Invented does plumbing.'}]})
        self.assertEqual(result['contact_name'], '')

    def test_email_name_survives_model_failure(self):
        result = research_contacts(['https://example.com'], lambda url: ('Contact jon@example.com', url),
            app.parse_page, app._is_directory_url, Mock(side_effect=ValueError('invalid JSON')),
            True, True, 'Plumber Jon')
        self.assertEqual(result['contact_name'], 'Jon')
        self.assertEqual(result['contact_research_status'], 'partial_error')

    def test_name_option_does_not_require_email_export(self):
        result, _ = self.research('Email jon@example.com', find_email=False)
        self.assertEqual(result['contact_name'], 'Jon')
        self.assertEqual(result['email'], '')


if __name__ == '__main__':
    unittest.main()
