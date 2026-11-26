"""Keep large indexes scoped to active books and short-lived jobs."""
from contextlib import contextmanager
import hashlib
import json
import os
import signal
import time
import fcntl
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from placekeeper.errors import ReadFailure
from placekeeper.mapping.text import TextIndex
from placekeeper.plugins.contracts import Book, Plugin
from placekeeper.sync.engine import sync_book
from placekeeper.sync.job import run_books
from placekeeper.sync.worker import run_worker
from placekeeper.storage import private_json


class MemoryTests(unittest.TestCase):
    def test_coordinates_are_compact_but_large_positions_are_preserved(self):
        compact = TextIndex.make([('ﬁne', lambda n: n)])
        self.assertEqual(compact.text, 'fine')
        self.assertEqual(compact.coordinates.values.itemsize, 4)
        self.assertEqual(list(compact.coordinates), [0, 0, 1, 2])
        large = TextIndex.make([('ab', lambda n: 2**32-1+n)])
        self.assertEqual(large.coordinates.values.itemsize, 8)
        self.assertEqual(list(large.coordinates), [2**32-1, 2**32])
        with self.assertRaises(OverflowError):
            TextIndex.make([('a', lambda n: -1)])

    def test_readers_share_coordinates_and_release_drops_cached_index(self):
        book = Book('key', 'Title', 'edition', epub=Path('fixture.epub'))
        with patch('placekeeper.mapping.text.epub_index', side_effect=[
                SimpleNamespace(coordinates=[1, 2]), SimpleNamespace(coordinates=[3, 4])]) as load:
            first = book.coordinates
            self.assertIs(first, book.coordinates)
            load.assert_called_once()
            book.release()
            self.assertEqual(book.coordinates, [3, 4])
            self.assertEqual(load.call_count, 2)
        mapper = SimpleNamespace(epub=SimpleNamespace(coordinates=first))
        mapped = Book('key', 'Title', 'edition', mapper=mapper)
        self.assertIs(mapped.coordinates, first)

    def test_paused_book_never_loads_map_and_resuming_revalidates_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            load = Mock(return_value=object())
            book = Book('key', 'Title', 'edition', load_mapper=load)
            sync_book(root, book, {}, mode='paused')
            load.assert_not_called()
            sync_book(root, book, {}, mode='reading')
            load.assert_called_once()
            book.release()
            self.assertIsNone(book.mapper)
            sync_book(root, book, {}, mode='reading')
            self.assertEqual(load.call_count, 2)

    def test_previous_map_is_released_even_when_catalog_retains_books(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = Book('first', 'First', 'edition', load_mapper=lambda:object())
            def load_second():
                self.assertIsNone(first.mapper)
                return object()
            second = Book('second', 'Second', 'edition', load_mapper=load_second)
            catalog = Plugin('test', 'Test', (), books=lambda _:iter([first, second]))
            result = run_books(root, {'test':catalog}, lambda _:None, dry_run=True)
            self.assertTrue(all(book['status']=='skipped' for book in result))
            self.assertIsNone(first.mapper)
            self.assertIsNone(second.mapper)

    def test_failed_reader_also_releases_map(self):
        with tempfile.TemporaryDirectory() as directory:
            book = Book('key', 'Title', 'edition', load_mapper=lambda:object())
            @contextmanager
            def fail(root, book):
                raise ReadFailure('Cannot read')
                yield
            plugin = Plugin('test', 'Test', (), reader=fail, books=lambda _:iter([book]))
            result = run_books(Path(directory), {'test':plugin}, lambda _:None, dry_run=True)
            self.assertEqual(result[0]['status'], 'error')
            self.assertIsNone(book.mapper)

    def test_deferred_map_still_rejects_changed_input(self):
        from placekeeper.plugins.kindle.legacy_catalog import books
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            epub = root/'book.epub'
            epub.write_bytes(b'changed')
            profile = root/'profile.json'
            private_json(profile, {'schema_version': 3, 'document': 'DOCUMENT1234',
                'kind': 'PDOC', 'x4_filename': 'Title.epub',
                'crosspoint_document': hashlib.md5(b'Title.epub').hexdigest(),
                'files': {'epub': {'path': str(epub), 'sha256': hashlib.sha256(b'original').hexdigest()},
                          'chapters': {'path': str(root/'chapters.json'), 'sha256': 'unused'}}})
            private_json(root/'onboarding/ready-profiles.json', {'profiles': [
                {'path': str(profile), 'sha256': hashlib.sha256(profile.read_bytes()).hexdigest()}]})
            book = next(books(root))
            self.assertIsNone(book.mapper)
            self.assertEqual(sync_book(root, book, {}, mode='paused')['writes'], 0)
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                sync_book(root, book, {}, mode='reading')

    @contextmanager
    def worker_module(self, source):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root/'placekeeper/sync'
            package.mkdir(parents=True)
            (root/'placekeeper/__init__.py').touch()
            (package/'__init__.py').touch()
            (package/'worker.py').write_text(source)
            with patch('placekeeper.sync.worker.ROOT', root):
                yield root

    def test_worker_failures_do_not_report_success(self):
        for source in ('print("invalid JSON")', 'print("[]")',
                       'import json; print(json.dumps({"result": []})); raise SystemExit(1)'):
            with self.worker_module(source) as root:
                with self.assertRaises(ReadFailure):
                    run_worker(root, 'progress', lambda _:None)
        phases = []
        with self.worker_module('import json; print(json.dumps({"phase": "Checking"})); print(json.dumps({"result": []}))') as root:
            self.assertEqual(run_worker(root, 'progress', phases.append), [])
        self.assertEqual(phases, ['Checking'])

    def test_worker_timeout_also_stops_descendants(self):
        source = ("import json,subprocess,sys,time,pathlib\n"
                  "code=\"import fcntl,pathlib,time; f=open('descendant.lock','w'); fcntl.flock(f,fcntl.LOCK_EX); pathlib.Path('descendant.ready').touch(); time.sleep(60)\"\n"
                  "child=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                  "while not pathlib.Path('descendant.ready').exists(): time.sleep(.01)\n"
                  "print(json.dumps({'phase':str(child.pid)}),flush=True)\n"
                  "time.sleep(60)\n")
        phases = []
        with self.worker_module(source) as root:
            try:
                with self.assertRaisesRegex(ReadFailure, 'timed out'):
                    run_worker(root, 'progress', phases.append, timeout=1.5)
                self.assertTrue(phases)
                with (root/'descendant.lock').open() as lock:
                    deadline = time.monotonic()+2
                    while True:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(.01)
            finally:
                if phases:
                    try:os.kill(int(phases[0]), signal.SIGKILL)
                    except ProcessLookupError:pass

    def test_real_worker_empty_catalog_and_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            phases = []
            self.assertEqual(run_worker(root, 'progress', phases.append, dry_run=True), [])
            with self.assertRaisesRegex(ReadFailure, 'timed out'):
                run_worker(root, 'progress', phases.append, dry_run=True, timeout=.001)
