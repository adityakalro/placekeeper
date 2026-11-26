"""Reader pairs can be discovered independently of Kindle mapping."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from placekeeper.plugins.library.books import books, discover


class PairCatalogTests(unittest.TestCase):
    def test_x4_libby_pair_is_discovered_without_kindle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            epub = root/'reader-books'/('a'*28+'.epub')
            epub.parent.mkdir()
            epub.write_bytes(b'fixture')
            source = Mock()
            record = {'id': 'a'*28, 'title': 'Sample', 'author': 'Author'}
            source.books.return_value = [record]
            source.download.return_value = epub
            loan = {'id': 'loan', 'title': 'Sample', 'firstCreatorName': 'Author'}
            with patch('placekeeper.plugins.library.books.LibrarySource', return_value=source),                     patch('placekeeper.plugins.library.books.active_loans', return_value=[loan]),                     patch('placekeeper.plugins.library.books.x4_loans', return_value=[record]):
                report = discover(root, {'library', 'libby', 'crosspoint'})
                discover(root, {'library', 'libby', 'crosspoint'})
                self.assertEqual(source.download.call_count, 1)
                epub.write_bytes(b'corrupted')
                discover(root, {'library', 'libby', 'crosspoint'})
                self.assertEqual(source.download.call_count, 2)
            self.assertEqual(report['matches'][0]['services'], ['crosspoint', 'libby'])
            discovered = list(books(root))
            self.assertEqual(len(discovered), 1)
            self.assertNotIn('kindle', discovered[0].bindings)
            self.assertIn('crosspoint', discovered[0].bindings)
