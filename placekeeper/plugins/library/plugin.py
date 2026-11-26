"""Always available local EPUB source."""
from placekeeper.plugins.contracts import Plugin
from placekeeper.plugins.lazy import deferred
PLUGIN=Plugin('library','EPUB library',(), 'Upload EPUBs on the Sync page; download them through the X4 OPDS browser.',
    books=deferred('placekeeper.plugins.library.books','books'),
    discover=deferred('placekeeper.plugins.library.books','discover'))
