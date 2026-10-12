import unittest

from business_classifier_app import auto_map_columns, column_preview, mapping_warnings, MappingConfig


class MappingPreviewTests(unittest.TestCase):
    def test_empty_alias_not_selected(self):
        headers = ["Title", "business_name", "website", "other"]
        rows = [["", "Example Concrete", "https://example.com", "value"]]
        mapping = auto_map_columns(headers, rows)
        self.assertEqual(mapping["business_name"], "business_name")
        self.assertNotIn("Title", mapping.values())

    def test_unknown_headers_require_manual_name_choice(self):
        self.assertEqual(auto_map_columns(["Title", "Score1"], [["", "Acme"]])["business_name"], "")

    def test_preview_handles_sparse_rows(self):
        self.assertEqual(column_preview(["Name", "URL"], [["Acme"], ["", ""]], "URL"),
                         "EMPTY — 0/2 rows populated")
        self.assertIn("1/2 populated", column_preview(["Name"], [["Acme"], [""]], "Name"))
        self.assertIn("Acme", column_preview(["Name"], [["Acme"]], "Name"))

    def test_missing_selection(self):
        self.assertIn("Choose a column", column_preview(["Name"], [["Acme"]], "old header"))

    def test_content_beats_sparse_scraper_headers(self):
        headers = ['Score', 'Score1', 'hidden', 'hidden1', 'Title1', 'm1', 'linkA', 'linkB']
        rows = [["Sparse Co" if i == 0 else "", f"Concrete Company {i}", "", "(713) 555-1234",
                 f"{i + 1} Main St", "Dallas, TX", "https://manta.com/c/example",
                 "https://manta.com/urlverify?redirect=https%3A%2F%2Fexample.com"] for i in range(100)]
        result = auto_map_columns(headers, rows)
        self.assertEqual(result['business_name'], 'Score1')
        self.assertEqual(result['phone'], 'hidden1')
        self.assertEqual(result['street'], 'Title1')
        self.assertEqual(result['city_state'], 'm1')
        self.assertEqual(result['url_1'], 'linkB')
        self.assertEqual(result['url_2'], 'linkA')
        renamed = [f'field_{i}' for i in range(len(headers))]
        self.assertEqual(auto_map_columns(renamed, rows)['business_name'], 'field_1')

    def test_ambiguous_text_is_not_guessed(self):
        self.assertEqual(auto_map_columns(['a', 'b'], [['Acme', 'John Smith'],
                        ['Beta', 'Jane Jones']])['business_name'], '')

    def test_sparse_name_warning(self):
        config = MappingConfig(business_name='Name', phone='', street='', city_state='', url_1='', url_2='')
        warnings = mapping_warnings(['Name'], [['Acme'], ['']], config, True)
        self.assertIn('1/2', warnings[0])
        self.assertIn('2/2', warnings[1])

    def test_comma_in_excel_style_cell_is_not_split(self):
        result = auto_map_columns(['Company', 'Phone'], [['Acme, LLC', '713-555-1234']])
        self.assertEqual(result['business_name'], 'Company')


if __name__ == "__main__":
    unittest.main()
