import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from placekeeper.mapping.text import Mapper, TextIndex, epub_index, epub_position_details


class MappingTests(unittest.TestCase):
    def test_compact_xpath_coordinates_lookup_and_whitespace_snap(self):
        index=TextIndex.make([(' A ﬁ',lambda o:f'/body/p[1]/text()[1].{o}'),
                              ('tail',lambda o:f'/body/p[1]/text()[2].{o}')])
        coords=index.coordinates
        self.assertEqual(coords[0],'/body/p[1]/text()[1].1')
        self.assertEqual(coords.index('/body/p[1]/text()[1].3'),1)
        self.assertEqual(coords.at_or_after('/body/p[1]/text()[1].2'),1)
        self.assertEqual(coords[3],'/body/p[1]/text()[2].0')
        self.assertEqual(coords[-1],'/body/p[1]/text()[2].3')
        with self.assertRaises(ValueError):coords.at_or_after('/body/p[9]/text()[1].0')
        with self.assertRaises(ValueError):coords.at_or_after('/body/p[1]/text()[1].99')
        with self.assertRaises(IndexError):_ = coords[-len(coords)-1]

    def test_unicode_coordinates_survive_normalization(self):
        index = TextIndex.make([(' “ﬁne”\n', lambda o: o)])
        self.assertEqual(index.text, '"fine"')
        self.assertEqual(index.coordinates, [1, 2, 2, 3, 4, 5])

    def test_duplicate_passage_is_rejected(self):
        a = TextIndex.make([('a'*250, lambda o: o)])
        b = TextIndex.make([('a'*501, lambda o: o)])
        with self.assertRaisesRegex(ValueError, 'unique'):
            a.unique_match(b, 100)

    def test_unknown_edition_is_rejected(self):
        a = TextIndex.make([('x'*250, lambda o: o)])
        b = TextIndex.make([('y'*250, lambda o: o)])
        with self.assertRaises(ValueError):
            a.unique_match(b, 100)

    def test_inline_text_nodes_and_bucket_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            epub = root/'book.epub'
            paragraph = 'A sufficiently distinctive paragraph with Unicode “quotes” and inline text.'
            with ZipFile(epub, 'w') as z:
                z.writestr('META-INF/container.xml', '<container><rootfile full-path="OEBPS/book.opf"/></container>')
                z.writestr('OEBPS/book.opf', '<package><manifest><item id="a" href="a.xhtml"/></manifest><spine><itemref idref="a"/></spine></package>')
                z.writestr('OEBPS/a.xhtml', f'<html><body><p>{paragraph}<em>Emphasis</em>Tail</p></body></html>')
            content = root/'content.json'
            content.write_text(json.dumps({'data': [{'type': 1, 'position': 101, 'content': paragraph+'EmphasisTail'}]}))
            locations = root/'locations.json'
            locations.write_text(json.dumps([{'location': 1, 'position': 100}, {'location': 2, 'position': 100}, {'location': 3, 'position': 200}]))
            m = Mapper(epub, content, locations)
            xpath = '/body/DocFragment[1]/body/p[1]/text()[1].0'
            result = m.from_xpointer(xpath)
            self.assertEqual(result['sidecar_range'], [150, 299])
            self.assertEqual(m.from_sidecar(299)['xpointer'], xpath)
            self.assertIn('/body/DocFragment[1]/body/p[1]/em[1]/text()[1].0', m.epub.coordinates)
            self.assertIn('/body/DocFragment[1]/body/p[1]/text()[2].0', m.epub.coordinates)
            details = epub_position_details(epub, '/body/DocFragment[1]/body/p[1]/text()[2].1')
            self.assertEqual(details['spine'], 0)
            self.assertEqual(details['visible_text_offset'], len(paragraph)+len('Emphasis')+1)
            self.assertAlmostEqual(details['percentage'], (len(paragraph)+len('Emphasis')+1)/len(paragraph+'EmphasisTail'))
            with self.assertRaises(ValueError):
                epub_position_details(epub, '/body/DocFragment[1]/body/p[1]/text()[2].999')
            with self.assertRaises(ValueError):
                m.from_sidecar(99999)
            with self.assertRaises(ValueError):
                m.from_xpointer('/body/DocFragment[9]/body/p[1]/text()[1].0')


class PrivateBookTests(unittest.TestCase):
    def test_confirmed_book_handoffs_in_both_directions(self):
        import os
        from placekeeper.plugins.kindle.profiles import load_profile

        path = os.environ.get('PLACEKEEPER_TEST_PROFILE')
        if not path or not Path(path).is_file():
            self.skipTest('Optional private validation profile is not configured')
        profile, mapper, _ = load_profile(path)
        verified = profile['physical_handoff_check']
        anchor = verified['predicted']['xpointer']
        position = verified['observed_successful_sidecar_write']
        self.assertEqual(mapper.from_sidecar(position)['xpointer'], anchor)
        first, last = mapper.from_xpointer(anchor)['sidecar_range']
        self.assertLessEqual(first, position)
        self.assertLessEqual(position, last)


if __name__ == '__main__':
    unittest.main()
