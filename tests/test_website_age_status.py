import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from unittest.mock import Mock

import business_classifier_app as app


class AgeStatusTests(unittest.TestCase):
    def test_working_business_site_skips_directory(self):
        fetch = Mock(return_value=('<p>Welcome to our business.</p>', 'https://example.com/'))
        result = app.find_business_age(['https://manta.com/c/example', 'https://example.com/'], fetch)
        self.assertEqual(result['age_status'], 'not_found')
        self.assertEqual(result['error'], '')
        fetch.assert_called_once_with('https://example.com/')

    def test_redirect_target_is_used_without_loading_directory(self):
        fetch = Mock(return_value=('Established in 1984', 'https://example.com/'))
        result = app.find_business_age([
            'https://manta.com/urlverify?redirect=https%3A%2F%2Fexample.com%2F'], fetch)
        fetch.assert_called_once_with('https://example.com/')
        self.assertEqual(result['age_status'], 'found')
        self.assertEqual(result['founded_year'], '1984')

    def test_blocked_status_codes(self):
        for code in (401, 403, 429):
            with self.subTest(code=code):
                fetch = Mock(side_effect=HTTPError('https://example.com', code, 'blocked', {}, None))
                self.assertEqual(app.find_business_age(['https://example.com'], fetch)['age_status'],
                                 'website_blocked')

    def test_unavailable_status_codes(self):
        for error in (URLError('DNS failed'), TimeoutError('timed out'),
                      HTTPError('https://example.com', 404, 'missing', {}, None)):
            result = app.find_business_age(['https://example.com'], Mock(side_effect=error))
            self.assertEqual(result['age_status'], 'website_unavailable')
            self.assertIn(str(error), result['error'])

    def test_about_failure_is_incomplete_not_whole_site_failure(self):
        fetch = Mock(side_effect=[('<a href="/about">About us</a>', 'https://example.com/'),
                                 HTTPError('https://example.com/about', 403, 'blocked', {}, None)])
        result = app.find_business_age(['https://example.com/', 'https://manta.com/c/example'], fetch)
        self.assertEqual(result['age_status'], 'age_check_incomplete')
        self.assertIn('https://example.com/about', result['error'])
        self.assertEqual(fetch.call_count, 2)

    def test_directory_fallback_still_works_if_business_site_unavailable(self):
        fetch = Mock(side_effect=[URLError('DNS failed'), ('Founded in 2001', 'https://manta.com/c/example')])
        result = app.find_business_age(['https://example.com/', 'https://manta.com/c/example'], fetch)
        self.assertEqual(result['age_status'], 'found')
        self.assertEqual(result['founded_year'], '2001')
        self.assertEqual(result['error'], '')

    def test_no_url(self):
        fetch = Mock()
        self.assertEqual(app.find_business_age([''], fetch)['age_status'], 'no_url')
        fetch.assert_not_called()


class PageCacheTests(unittest.TestCase):
    def test_reuse_and_redirect_alias(self):
        fetch = Mock(return_value=('page', 'https://www.example.com/'))
        cache = app.PageFetchCache(fetch)
        self.assertEqual(cache('https://example.com/'), cache('https://www.example.com/'))
        cache('https://example.com/#section')
        self.assertEqual(fetch.call_count, 1)

    def test_bounded_eviction(self):
        fetch = Mock(side_effect=lambda url: ('page', url))
        cache = app.PageFetchCache(fetch, capacity=2)
        for path in ('a', 'b', 'c', 'a'):
            cache('https://example.com/' + path)
        self.assertEqual(fetch.call_count, 4)
        self.assertLessEqual(len(cache.pages), 2)

    def test_failure_is_not_permanently_cached(self):
        fetch = Mock(side_effect=[URLError('offline'), ('page', 'https://example.com/')])
        cache = app.PageFetchCache(fetch)
        with self.assertRaises(URLError):
            cache('https://example.com/')
        self.assertEqual(cache('https://example.com/')[0], 'page')

    def test_simultaneous_requests_share_one_fetch(self):
        started, release = threading.Event(), threading.Event()
        def fetch(url):
            started.set()
            self.assertTrue(release.wait(3))
            return ('page', url)
        wrapped = Mock(side_effect=fetch)
        cache = app.PageFetchCache(wrapped)
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(cache, 'https://example.com/')
            self.assertTrue(started.wait(3))
            two = pool.submit(cache, 'https://example.com/')
            release.set()
            self.assertEqual(one.result(), two.result())
        wrapped.assert_called_once()


if __name__ == '__main__':
    unittest.main()
