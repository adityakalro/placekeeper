"""Unchanged audio maps should not index the book again."""
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from placekeeper.plugins.libby.validation import validate_books


class AudioValidationTests(unittest.TestCase):
    def test_reuse_and_invalidate_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            epub = root/'book.epub'
            epub.write_bytes(b'book')
            profile = {'x4_filename': 'Book.epub', 'files': {'epub': {
                'path': str(epub), 'sha256': sha256(b'book').hexdigest()}}}
            client = Mock()
            client.open_player.return_value = (Mock(), None, {'chapters': [], 'duration': 100})
            with patch('placekeeper.plugins.libby.validation.Libby', return_value=client), \
                    patch('placekeeper.plugins.libby.validation.validate_chapters', return_value=9) as validate:
                self.assertEqual(validate_books(root, [profile])[0]['chapter_checks'], 9)
                validate_books(root, [profile])
                self.assertEqual(validate.call_count, 1)
                client.open_player.return_value = (Mock(), None, {'chapters': [], 'duration': 101})
                validate_books(root, [profile])
                self.assertEqual(validate.call_count, 2)
                (root/'audio-validation.json').write_text('invalid JSON')
                validate_books(root, [profile])
                self.assertEqual(validate.call_count, 3)
                epub.write_bytes(b'changed')
                self.assertEqual(validate_books(root, [profile])[0]['status'], 'audio_mapping_required')
                self.assertEqual(validate.call_count, 3)
