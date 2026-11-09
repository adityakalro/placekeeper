"""Check protocol behavior in the production progress clients."""
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree

import httpx

from placekeeper.errors import ReadFailure
from placekeeper.plugins.crosspoint.client import crosspoint_progress, read_progress
from placekeeper.plugins.kindle.progress import (
    kindle_position, parse_progress_time, read_sidecar, write_position,
)
from placekeeper.plugins.kindle.reader import KindleReader
from placekeeper.plugins.transport import json_reader

P1 = '/body/DocFragment[1]/body/p[1]/text()[1].0'


class ProgressClientTests(unittest.TestCase):
    def test_wrong_book_and_missing_progress_fail_closed(self):
        with self.assertRaises(ReadFailure):
            kindle_position({'payload': {'key': 'wrong', 'type': 'PDOC'}}, 'book')
        with self.assertRaises(ReadFailure):
            kindle_position({'payload': {'key': 'book', 'type': 'PDOC', 'records': []}}, 'book')
        with self.assertRaises(ReadFailure):
            crosspoint_progress({'document': 'wrong', 'progress': P1}, 'book')
        with self.assertRaises(ReadFailure):
            crosspoint_progress({'document': 'book', 'progress': '23%'}, 'book')
        for payload in (None, [], {'key': 'book', 'type': 'PDOC', 'records': [None]}):
            with self.assertRaises(ReadFailure):
                kindle_position({'payload': payload}, 'book')

    def test_amazon_relative_dates_use_server_utc_day(self):
        xml = '<book><last_read pos="150" method="FRL">Furthest at 11:59 PM GMT yesterday.</last_read></book>'
        result = parse_progress_time(xml, 'Sat, 07 Nov 2026 04:45:02 GMT', 150)
        self.assertEqual(result.isoformat(), '2026-11-06T23:59:00+00:00')
        absolute = xml.replace('11:59 PM GMT yesterday', '10:42 PM GMT on November 05, 2026')
        self.assertEqual(parse_progress_time(absolute, 'Sat, 07 Nov 2026 04:45:02 GMT', 150).isoformat(),
                         '2026-11-05T22:42:00+00:00')
        with self.assertRaises(ValueError):
            parse_progress_time(xml, 'Sat, 07 Nov 2026 04:45:02 GMT', 300)

    def test_reads_use_get_and_keep_credentials_with_their_service(self):
        def respond(request):
            self.assertEqual(request.method, 'GET')
            if request.url.host == 'cde-ta-g7g.amazon.com':
                self.assertEqual(request.url.params['key'], 'KINDLEBOOK')
                self.assertNotIn('x-auth-key', request.headers)
                return httpx.Response(200, json={'payload': {'key': 'KINDLEBOOK', 'type': 'PDOC',
                                      'records': [{'type': 'kindle.lpr', 'location': '320'}]}})
            self.assertEqual(request.url.host, 'sync.crosspointreader.com')
            self.assertEqual(request.url.path, '/syncs/progress/cp-book')
            self.assertEqual(request.headers['x-auth-key'], 'secret-hash')
            return httpx.Response(200, json={'document': 'cp-book', 'progress': P1, 'timestamp': 1})
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            get = json_reader(client)
            self.assertEqual(kindle_position(read_sidecar(get, None, 'KINDLEBOOK'), 'KINDLEBOOK'), 320)
            data = read_progress(get, {'x-auth-key': 'secret-hash'}, 'cp-book')
            self.assertEqual(crosspoint_progress(data, 'cp-book'), P1)

    def test_transient_read_error_retries_and_http_error_omits_response(self):
        requests = []
        def respond(request):
            requests.append(request.method)
            if len(requests) == 1:
                raise httpx.ConnectError('DNS busy')
            return httpx.Response(401, text='sensitive body')
        with httpx.Client(transport=httpx.MockTransport(respond)) as client, \
                patch('placekeeper.plugins.transport.time.sleep'):
            with self.assertRaisesRegex(ReadFailure, r'^Kindle read failed: HTTP 401\.$'):
                read_sidecar(json_reader(client), None, 'book')
        self.assertEqual(requests, ['GET', 'GET'])

    def test_kindle_store_position_works_without_timestamp_and_recognizes_pending_write(self):
        mapper = Mock()
        mapper.from_sidecar.return_value = {'xpointer': P1}
        mapper.epub.coordinates.at_or_after.return_value = 1
        reader = KindleReader(Mock(), None, 'book', mapper, 'EBOK')
        sidecar = {'payload': {'key': 'book', 'type': 'EBOK',
                               'records': [{'type': 'kindle.lpr', 'location': 449}]}}
        with patch('placekeeper.plugins.kindle.reader.read_sidecar', return_value=sidecar):
            hint = {'need_timestamp': False, 'receipt': None, 'pending': {'target': 449, 'stamp': 5000}}
            result = reader.read(hint)
            self.assertTrue(result.own)
            self.assertEqual(result.stamp, 5000)
            sidecar['payload']['records'][0]['location'] = 600
            result = reader.read(hint)
            self.assertFalse(result.own)
            self.assertEqual(result.native, 600)
            self.assertIsNone(result.stamp)

    def test_kindle_write_only_updates_last_read(self):
        def respond(request):
            self.assertEqual(request.method, 'POST')
            book = ElementTree.fromstring(request.content).find('book')
            self.assertEqual(book.attrib['key'], 'KINDLEBOOK')
            self.assertEqual(book.attrib['guid'], 'test-guid')
            self.assertEqual([entry.tag for entry in book], ['last_read'])
            self.assertEqual(book.find('last_read').attrib['begin'], '299')
            return httpx.Response(200)
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            write_position(client, None, 'KINDLEBOOK', 'test-guid', 299)
