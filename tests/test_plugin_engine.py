"""Exercise the production job with an installed fourth reader."""
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import re
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import Mock, patch

import httpx

from placekeeper import plugins
from placekeeper.errors import MissingProgress, ReadFailure, UnsupportedMapping
from placekeeper.plugins.contracts import Book, Observation, Plugin
from placekeeper.plugins.kindle.legacy_catalog import legacy_state
from placekeeper.plugins.libby.client import Libby
from placekeeper.plugins.libby.reader import LibbyReader, open_reader as open_libby
from placekeeper.plugins.registry import installed
from placekeeper.sync.engine import execute
from placekeeper.sync.job import run_books
from placekeeper.sync.planner import choose_source
from placekeeper.sync.recovery import main as recovery
from placekeeper.storage import state_lock
from placekeeper.web.app import Jobs
from tests import test_sync_page

FAKE_PLUGIN = '''
from contextlib import contextmanager
import json
from placekeeper.plugins.contracts import Book, Observation, Plugin
from placekeeper.storage import private_json

FILE = 'fourth-reader.json'

class Reader:
    def __init__(self, path):
        self.path = path

    def read(self, hint):
        value = json.loads(self.path.read_text())
        return Observation(value, value)

    def equivalent(self, first, second):
        return first == second

    def target(self, anchor, current):
        return anchor

    def write(self, target):
        private_json(self.path, target)

@contextmanager
def reader(root, book):
    yield Reader(root / FILE)

def setup(root, pending, action, data):
    private_json(root / FILE, int(data['start_position']))
    return {'connected': True}

def books(root):
    yield Book('fixture-edition', 'Fixture', 'verified-map')

PLUGIN = Plugin('fourth', 'Fourth Reader', (FILE,),
    actions=({'id':'connect','label':'Connect Fourth Reader',
              'fields':[{'name':'start_position','label':'Initial position'}]},),
    setup=setup, reader=reader, books=books)
'''


class Reader:
    def __init__(self, position, stamp=None):
        self.position = position
        self.stamp = stamp
        self.writes = []

    def read(self, hint):
        return Observation(self.position, self.position, self.stamp)

    def equivalent(self, first, second):
        return first == second

    def target(self, anchor, current):
        return anchor

    def write(self, target):
        self.writes.append(target)
        self.position = target


def with_reader(plugin, reader):
    @contextmanager
    def open_reader(root, book):
        yield reader
    return replace(plugin, reader=open_reader, reader_session=None)


class PluginEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'journal.json'

    def load_fourth(self):
        directory = self.root/'extensions'
        module = directory/'fake_fourth'
        module.mkdir(parents=True)
        (module/'__init__.py').touch()
        (module/'plugin.py').write_text(FAKE_PLUGIN)
        self.addCleanup(sys.modules.pop, 'placekeeper.plugins.fake_fourth.plugin', None)
        self.addCleanup(sys.modules.pop, 'placekeeper.plugins.fake_fourth', None)
        with patch.object(plugins, '__path__', [*plugins.__path__, str(directory)]):
            return installed()

    def connect_through_setup(self, jobs):
        page = test_sync_page.PageTests().handler(jobs, '/setup', origin='http://127.0.0.1:8780',
            body=b'{"service":"fourth","action":"connect","consent":true,"start_position":"200"}')
        page.do_GET()
        token = re.search(rb"const csrf='([^']+)'", page.wfile.getvalue())[1].decode()
        page.headers['X-Placekeeper'] = token
        page.path = '/api/setup'
        page.do_POST()
        self.assertEqual(page.code, 200)
        self.assertTrue(jobs.status()['services']['fourth']['connected'])

    def test_installed_fourth_plugin_connects_and_syncs_through_normal_job(self):
        registry = self.load_fourth()
        readers = {key: Reader(position) for key, position in [('kindle', 500), ('crosspoint', 100), ('libby', 300)]}
        for key, reader in readers.items():
            registry[key] = with_reader(registry[key], reader)
            for file in registry[key].files:
                (self.root/file).touch()
        jobs = Jobs(self.root, plugins=registry)
        self.assertEqual(jobs.status()['services']['fourth']['actions'][0]['label'], 'Connect Fourth Reader')
        self.connect_through_setup(jobs)
        self.assertTrue(jobs.start())
        self.assertTrue(jobs.lock.acquire(timeout=5))
        jobs.lock.release()
        self.assertEqual(jobs.last['status'], 'complete')
        self.assertEqual(jobs.last['books'][0]['writes'], 3)
        self.assertEqual(json.loads((self.root/'fourth-reader.json').read_text()), 500)

    def test_plugin_catalog_works_without_kindle_or_readest_and_preview_matches_live(self):
        registry = self.load_fourth()
        reader = Reader(600)
        registry['crosspoint'] = with_reader(registry['crosspoint'], reader)
        (self.root/'crosspoint-auth.json').touch()
        jobs = Jobs(self.root, plugins=registry)
        self.connect_through_setup(jobs)
        self.assertTrue(jobs.connections.ready())
        connected = jobs.connections.connected()
        preview = run_books(self.root, connected, lambda _: None, dry_run=True)[0]
        self.assertEqual(preview['targets'], {'fourth': 600})
        self.assertEqual(json.loads((self.root/'fourth-reader.json').read_text()), 200)
        result = jobs.sync_books(lambda _: None)[0]
        self.assertEqual(result['writes'], 1)
        self.assertEqual(json.loads((self.root/'fourth-reader.json').read_text()), 600)
        self.assertEqual(jobs.sync_books(lambda _: None)[0]['writes'], 0)

    def test_kindle_syncs_with_either_available_partner(self):
        for missing, partner in [('crosspoint', 'libby'), ('libby', 'crosspoint')]:
            with self.subTest(missing=missing):
                kindle = Reader(10, 100)
                other = Reader(100, 300)
                absent = Mock()
                absent.read.side_effect = MissingProgress('No saved position')
                result = execute({'kindle': kindle, partner: other, missing: absent},
                                 {'receipts': {}}, self.path)
                self.assertEqual(result['status'], 'synced')
                self.assertEqual(result['source'], partner)
                self.assertEqual(kindle.writes, [100])
                absent.write.assert_not_called()

    def test_read_error_blocks_writes_but_missing_reader_does_not(self):
        first, second = Reader(10), Reader(100)
        optional = Mock()
        readers = {'a': first, 'b': second, 'c': optional}
        optional.read.side_effect = ReadFailure('Unavailable reader')
        self.assertEqual(execute(readers, {'receipts': {}}, self.path)['status'], 'error')
        self.assertEqual(first.writes, [])
        optional.read.side_effect = MissingProgress('No position')
        self.assertEqual(execute(readers, {'receipts': {}}, self.path)['writes'], 1)
        self.assertEqual(first.writes, [100])

    def test_interrupted_write_is_not_retried(self):
        reader = Reader(10)
        reader.write = Mock(side_effect=ReadFailure('Request outcome unknown'))
        readers = {'a': reader, 'b': Reader(100)}
        state = {'receipts': {}}
        self.assertEqual(execute(readers, state, self.path)['status'], 'unconfirmed')
        self.assertEqual(execute(readers, state, self.path)['status'], 'error')
        reader.write.assert_called_once()

    def test_recovery_resumes_partial_handoff_from_original_source(self):
        class Rounded(Reader):
            def read(self, hint):
                rounded = self.position == 200
                return Observation(self.position, self.position + int(rounded), own=rounded)

        destination = Reader(50)
        readers = {'kindle': Reader(200), 'x4': Rounded(100), 'libby': destination}
        with patch.object(destination, 'write', side_effect=OSError('write interrupted')):
            path = self.root/'progress'/('a'*64 + '.json')
            result = execute(readers, {'receipts': {}}, path)
            self.assertEqual(result['outcomes'], {'x4': 'verified'})
            self.assertEqual(result['status'], 'unconfirmed')
            self.assertEqual(execute(readers, json.loads(path.read_text()), path)['writes'], 0)
        with patch.object(sys, 'argv', ['recovery', '--data-root', str(self.root), '--clear', path.stem]), \
                patch('builtins.print'):
            recovery()
        result = execute(readers, json.loads(path.read_text()), path)
        self.assertEqual(result['source'], 'kindle')
        self.assertEqual(result['outcomes'], {'x4': 'kept', 'libby': 'verified'})
        self.assertEqual(destination.writes, [200])
        self.assertNotIn('handoff', json.loads(path.read_text()))

    def test_parallel_reads_overlap_before_preview_returns(self):
        barrier = Barrier(3)

        class Concurrent(Reader):
            parallel_reads = True

            def read(self, hint):
                barrier.wait(timeout=2)
                return super().read(hint)

        readers = {'kindle': Concurrent(300), 'crosspoint': Concurrent(100), 'libby': Concurrent(200)}
        result = execute(readers, {'receipts': {}}, self.path, dry_run=True)
        self.assertEqual(result['targets'], {'crosspoint': 300, 'libby': 300})
        self.assertTrue(all(not reader.writes for reader in readers.values()))

    def test_unsupported_audio_mapping_allows_ebook_sync(self):
        kindle, x4, audio = Reader(200), Reader(100), Reader(300)
        with patch.object(audio, 'read', side_effect=UnsupportedMapping('Audio chapter is unmapped.')):
            result = execute({'kindle': kindle, 'x4': x4, 'libby': audio},
                             {'receipts': {}}, self.path)
        self.assertEqual(result['outcomes'], {'x4': 'verified'})
        self.assertEqual(result['skipped_readers'], {'libby': 'Audio chapter is unmapped.'})
        self.assertEqual(x4.writes, [200])
        self.assertEqual(audio.writes, [])

    def test_sync_uses_the_discovery_watcher_lock(self):
        directory = self.root/'onboarding'
        directory.mkdir()
        with state_lock(directory/'state.json'):
            with self.assertRaisesRegex(ValueError, 'Another loop'):
                run_books(self.root, {}, lambda _: None, dry_run=True)

    def test_own_furthest_position_is_not_echoed(self):
        old = {'receipts': {'Libby': {'position': 30, 'stamp': 3000}}}
        (self.root/'three-way-state.json').write_text(json.dumps(old))
        observations = {'kindle': Observation(10, 10),
                        'crosspoint': Observation(20, 20, 2990),
                        'libby': Observation(30, 30, 3000)}
        self.assertIsNone(choose_source(observations, legacy_state(self.root)['receipts']))
        self.assertEqual(choose_source(observations, {}), 'libby')

    def test_furthest_reader_wins_without_timestamp_and_updates_only_lagging_readers(self):
        kindle, x4, audio = Reader(200), Reader(160, 100), Reader(100, 300)
        result = execute({'kindle': kindle, 'crosspoint': x4, 'libby': audio},
                         {'receipts': {}}, self.path)
        self.assertEqual(result['source'], 'kindle')
        self.assertEqual(kindle.writes, [])
        self.assertEqual(x4.writes, [200])
        self.assertEqual(audio.writes, [200])

    def test_source_change_during_write_stops_remaining_destinations(self):
        first, second, source = Reader(10), Reader(20), Reader(100)
        write = first.write
        def change(target):
            write(target)
            source.position = 120
        first.write = change
        result = execute({'a': first, 'b': second, 'c': source}, {'receipts': {}}, self.path)
        self.assertEqual(result['status'], 'changed')
        self.assertEqual(result['outcomes'], {'a': 'verified'})
        self.assertEqual(second.writes, [])

    def test_audio_tolerance_is_sixty_seconds_in_both_comparisons(self):
        chapters = Mock()
        chapters.to_audio.side_effect = lambda coordinate: (coordinate, 'Chapter')
        reader = LibbyReader(Mock(), {}, Mock(), '', {}, chapters, range(100_000), None)
        current = Observation(0, 0)
        self.assertTrue(reader.equivalent(0, 60_000))
        self.assertFalse(reader.equivalent(0, 60_001))
        self.assertIsNone(reader.target(60_000, current))
        self.assertEqual(reader.target(60_001, current), 60_001)

    def test_ebook_sessions_reuse_connections_across_books(self):
        registry = installed()
        (self.root/'amazon-device.json').write_text('{}')
        (self.root/'crosspoint-auth.json').write_text('{"username":"test","auth_key":"test"}')
        for service in ('kindle', 'crosspoint'):
            with self.subTest(service=service), \
                    patch('placekeeper.plugins.kindle.reader.Authenticator.from_dict') as auth:
                with registry[service].reader_session(self.root) as open_reader:
                    client = None
                    for name in ('first', 'second', 'third'):
                        book = Book(name, name, 'map', {service: name}, self.root/'book.epub', Mock())
                        with open_reader(self.root, book) as reader:
                            if client is None:
                                client = reader.client
                            self.assertIs(reader.client, client)
                            self.assertFalse(client.is_closed)
                    self.assertEqual(auth.call_count, int(service == 'kindle'))
                self.assertTrue(client.is_closed)

    def test_one_libby_shelf_request_per_job(self):
        registry = installed()
        libby = Libby(self.root/'libby-device.json')
        catalog = Plugin('fixture', 'Fixture', (), books=lambda root: [
            Book(str(i), f'Title {i}', 'map', epub=root/'fixture.epub') for i in range(3)])
        with patch('placekeeper.plugins.libby.reader.Libby', return_value=libby), \
                patch.object(libby, 'get', return_value={'loans': []}) as get:
            results = run_books(self.root, {'fixture': catalog, 'libby': registry['libby']},
                                lambda _: None, dry_run=True)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(result['status'] == 'skipped' for result in results))
        get.assert_called_once_with('/chip/sync')
        self.assertTrue(libby.client.is_closed)

    def test_libby_session_closes_a_player_that_is_already_open(self):
        player = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
        self.addCleanup(player.close)
        player.get('https://example.invalid/')
        libby = Mock()
        libby.open_player.return_value = player, 'https://example.invalid/', {}
        book = Book('fixture', 'Fixture', 'map', {'libby': {'id': 'loan'}},
                    self.root/'fixture.epub', Mock())
        with patch('placekeeper.plugins.libby.reader.Libby', return_value=libby), \
                patch('placekeeper.plugins.libby.reader.ChapterMap'):
            with open_libby(self.root, book) as reader:
                self.assertIsInstance(reader, LibbyReader)
        self.assertTrue(player.is_closed)
        libby.close.assert_called_once()
