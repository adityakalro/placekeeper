"""Libby progress and chapter alignment against a verified edition."""
from contextlib import ExitStack, closing, contextmanager

from placekeeper.errors import MissingProgress, ReadFailure
from placekeeper.mapping.chapters import ChapterMap
from placekeeper.mapping.text import epub_index
from placekeeper.plugins.contracts import Observation
from .client import Libby, elapsed

AUDIO_TOLERANCE_MS = 60_000


class LibbyReader:
    prefer_recent = True

    def __init__(self, libby, loan, player, url, timing, chapters, coordinates, marker):
        self.libby, self.loan, self.player, self.url = libby, loan, player, url
        self.timing, self.chapters, self.coordinates = timing, chapters, coordinates
        self.initial = marker

    def read(self, hint):
        marker = self.initial if self.initial is not None else self.libby.position(self.loan)
        self.initial = None
        native = elapsed(self.timing, marker)
        coordinate, _ = self.chapters.to_text(native)
        stamp = marker.get('timestamp')
        if type(stamp) not in (int, float) or stamp <= 0:
            raise ReadFailure('Libby returned no valid progress timestamp.')
        if stamp >= 100_000_000_000:
            stamp /= 1000
        return Observation(native, self.coordinates.at_or_after(coordinate), stamp,
                           own=marker.get('deviceId') == self.libby.state.get('chipId'))

    def target(self, anchor, current):
        target, _ = self.chapters.to_audio(self.coordinates[anchor])
        return target if abs(target - current.native) > AUDIO_TOLERANCE_MS else None

    def equivalent(self, first, second):
        try:
            a, _ = self.chapters.to_audio(self.coordinates[first])
            b, _ = self.chapters.to_audio(self.coordinates[second])
        except ValueError:
            return False
        return abs(a - b) <= AUDIO_TOLERANCE_MS

    def write(self, target):
        self.libby.write(self.player, self.url, self.timing, target)


@contextmanager
def reader_session(root):
    with ExitStack() as stack:
        libby = None

        @contextmanager
        def open_book(_root, book):
            nonlocal libby
            if book.epub is None:
                yield None
                return
            if libby is None:
                libby = stack.enter_context(closing(Libby(root/'libby-device.json')))
            loan = book.bindings.get('libby') or libby.match(book.title, book.epub)
            if loan is None:
                yield None
                return
            try:
                marker = libby.position(loan)
            except MissingProgress:
                yield None
                return
            player, url, timing = libby.open_player(loan)
            with closing(player):
                coordinates = book.mapper.epub.coordinates if book.mapper else epub_index(book.epub).coordinates
                chapters = ChapterMap(book.epub, coordinates, timing)
                yield LibbyReader(libby, loan, player, url, timing, chapters, coordinates, marker)

        yield open_book


@contextmanager
def open_reader(root, book):
    with reader_session(root) as open_book, open_book(root, book) as reader:
        yield reader
