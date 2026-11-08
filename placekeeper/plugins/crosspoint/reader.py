"""CrossPoint progress in the verified EPUB's text coordinates."""
from contextlib import ExitStack, contextmanager
from functools import cache
import json

import httpx

from placekeeper.errors import MissingProgress, ReadFailure
from placekeeper.mapping.text import epub_index, epub_position_details
from placekeeper.plugins.contracts import Observation
from placekeeper.plugins.transport import check_write, json_reader
from .client import crosspoint_progress, is_own_progress, read_progress, write_progress


class CrossPointReader:
    prefer_recent = False

    def __init__(self, client, headers, document, epub, coordinates):
        self.client, self.headers, self.document = client, headers, document
        self.epub, self.coordinates = epub, coordinates
        self.get = json_reader(client, missing_status=404)

    def read(self, hint):
        data = read_progress(self.get, self.headers, self.document)
        if not data:
            raise MissingProgress('X4 has no saved position for this book.')
        native = crosspoint_progress(data, self.document)
        stamp = data.get('timestamp')
        if type(stamp) in (int, float) and stamp >= 100_000_000_000:
            stamp /= 1000
        if type(stamp) not in (int, float) or stamp <= 0:
            if hint['need_timestamp']:
                raise ReadFailure('X4 returned no valid progress timestamp.')
            stamp = None
        own = is_own_progress(data.get('device_id'), data.get('device'))
        return Observation(native, self.coordinates.at_or_after(native), stamp, own=own)

    def equivalent(self, first, second):
        return first == second

    def target(self, anchor, current):
        return self.coordinates[anchor]

    def write(self, target):
        percentage = epub_position_details(self.epub, target)['percentage']
        check_write(write_progress(self.client, self.headers, self.document, target, percentage), 'X4')


@contextmanager
def reader_session(root):
    with ExitStack() as stack:
        @cache
        def connect():
            credentials = json.loads((root/'crosspoint-auth.json').read_text())
            headers = {'x-auth-user': credentials['username'], 'x-auth-key': credentials['auth_key']}
            client = stack.enter_context(httpx.Client(timeout=20, follow_redirects=False))
            return client, headers

        @contextmanager
        def reader(root, book):
            binding = book.bindings.get('crosspoint')
            if not binding or book.epub is None:
                yield None
                return
            coordinates = book.mapper.epub.coordinates if book.mapper else epub_index(book.epub).coordinates
            client, headers = connect()
            yield CrossPointReader(client, headers, binding, book.epub, coordinates)

        yield reader


@contextmanager
def open_reader(root, book):
    with reader_session(root) as reader, reader(root, book) as opened:
        yield opened
