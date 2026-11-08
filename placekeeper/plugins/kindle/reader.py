"""Translate Kindle sidecar progress to verified text anchors."""
from contextlib import ExitStack, contextmanager
from functools import cache
import json

from audible import Authenticator
import httpx

from placekeeper.plugins.contracts import Observation
from placekeeper.plugins.transport import check_write, json_reader
from .progress import fetch_progress_time, kindle_position, read_sidecar, write_position


class KindleReader:
    prefer_recent = False

    def __init__(self, client, auth, document, mapper, kind="PDOC"):
        self.client, self.auth, self.document, self.mapper = client, auth, document, mapper
        self.kind = kind
        self.get = json_reader(client, missing_status=404 if kind == "EBOK" else None)

    def read(self, hint):
        self.sidecar = read_sidecar(self.get, self.auth, self.document, self.kind)
        position = kindle_position(self.sidecar, self.document, self.kind)
        coordinates = self.mapper.epub.coordinates
        anchor = coordinates.at_or_after(self.mapper.from_sidecar(position)['xpointer'])
        receipt, pending = hint['receipt'], hint['pending']
        if receipt and receipt['position'] == position:
            return Observation(position, anchor, receipt['stamp'], own=True)
        if pending and pending['target'] == position:
            return Observation(position, anchor, pending['stamp'], own=True)
        stamp = None
        if hint['need_timestamp']:
            stamp = fetch_progress_time(self, {'document': self.document, 'kind': self.kind}, self.sidecar, position).timestamp()
        return Observation(position, anchor, stamp, 60)

    def bucket(self, anchor):
        return self.mapper.from_xpointer(self.mapper.epub.coordinates[anchor])['sidecar_range']

    def equivalent(self, first, second):
        return first == second or self.bucket(first) == self.bucket(second)

    def target(self, anchor, current):
        low, high = self.bucket(anchor)
        return high if current.native < low else None

    def write(self, target):
        check_write(write_position(self.client, self.auth, self.document,
                                   self.sidecar['payload'].get('guid'), target, self.kind), 'Kindle')


@contextmanager
def reader_session(root):
    with ExitStack() as stack:
        @cache
        def connect():
            auth = Authenticator.from_dict(json.loads((root/'amazon-device.json').read_text()))
            client = stack.enter_context(httpx.Client(timeout=20, follow_redirects=False))
            return client, auth

        @contextmanager
        def reader(root, book):
            binding = book.bindings.get('kindle')
            if binding is None or book.mapper is None:
                yield None
                return
            client, auth = connect()
            yield KindleReader(client, auth, binding, book.mapper, book.bindings.get('kindle_kind', 'PDOC'))

        yield reader


@contextmanager
def open_reader(root, book):
    with reader_session(root) as reader, reader(root, book) as opened:
        yield opened
