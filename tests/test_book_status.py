"""Reading state controls must prevent writes without hiding real progress."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from placekeeper.plugins.contracts import Book, Observation, Plugin
from placekeeper.sync import book_status
from placekeeper.sync.engine import execute, sync_book
from placekeeper.sync.job import run_books
from placekeeper.web.app import Jobs


class Reader:
    def __init__(self, value, fraction):
        self.value, self.fraction = value, fraction
        self.writes = []

    def read(self, hint):
        return Observation(self.value, self.value, fraction=self.fraction)

    def target(self, anchor, current):
        return anchor

    def equivalent(self, first, second):
        return first == second

    def write(self, target):
        self.writes.append(target)
        self.value = target


class BookStatusTests(unittest.TestCase):
    def test_detection_considers_all_readers_and_unknown_fractions(self):
        def detect(*values):
            return book_status.automatic_status({str(i): Observation(i, i, fraction=f)
                                                 for i, f in enumerate(values)})
        self.assertEqual(detect(0, .0009), 'not_started')
        self.assertEqual(detect(0, .3), 'reading')
        self.assertEqual(detect(.2, .995), 'finished')
        for values in [(), (None,), (0, None), (float('nan'),), (1.1,)]:
            self.assertEqual(detect(*values), 'reading')

    def test_automatic_pause_prevents_writes_and_reading_override_allows_them(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            for fraction in (0, 1):
                readers = {'first': Reader(0, fraction), 'second': Reader(10, fraction)}
                result = execute(readers, {'receipts': {}}, path, mode='automatic')
                self.assertEqual(result['status'], 'paused')
                self.assertFalse(path.exists())
                self.assertEqual(readers['first'].writes, [])
            result = execute(readers, {'receipts': {}}, path, mode='reading')
            self.assertEqual(result['writes'], 1)
            self.assertEqual(readers['first'].writes, [10])

    def test_manual_pause_does_not_open_any_reader(self):
        plugin = Mock()
        for mode in ('paused', 'finished', 'not_started'):
            result = sync_book(Path('/unused'), Book('key', 'Title', 'edition'),
                               {'reader': plugin}, mode=mode)
            self.assertEqual(result['writes'], 0)
        plugin.reader.assert_not_called()

    def test_job_persists_override_and_automatic_resumes_when_started(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reader = Reader(0, 0)
            @contextmanager
            def open_reader(root, book):
                yield reader
            plugin = Plugin('fourth', 'Fourth', (), books=lambda _: [Book('key', 'Title', 'edition')],
                            reader=open_reader)
            def run(dry_run=False):
                return run_books(root, {'fourth': plugin}, lambda _: None, dry_run=dry_run)[0]
            self.assertEqual(run()['reading_status'], 'not_started')
            reader.fraction = .2
            self.assertNotEqual(run()['status'], 'paused')
            jobs = Jobs(root, plugins={'fourth': plugin})
            jobs.set_book_status('key', 'finished')
            self.assertEqual(run()['reading_status'], 'finished')
            self.assertEqual(Jobs(root, plugins={'fourth': plugin}).status()['books'][0]['mode'], 'finished')
            before = (root/'book-status.json').read_bytes()
            run(dry_run=True)
            self.assertEqual((root/'book-status.json').read_bytes(), before)
            for key, mode in [('missing', 'reading'), ('key', 'invalid')]:
                with self.assertRaises(ValueError):
                    jobs.set_book_status(key, mode)
            jobs.set_book_status('key', 'automatic')
            self.assertNotEqual(run()['status'], 'paused')

    def test_unconfirmed_write_is_not_bypassed_by_automatic_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            reader = Reader(0, 1)
            state = {'receipts': {}, 'pending': {'device': 'a', 'before': {'position': 0}, 'target': 10}}
            result = execute({'a': reader}, state, Path(directory)/'state.json', mode='automatic')
            self.assertEqual(result['status'], 'error')
            self.assertIn('unconfirmed', result['message'])
            self.assertEqual(reader.writes, [])
