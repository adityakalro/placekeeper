"""Readest content-provider registration."""
from placekeeper.plugins.contracts import Plugin
from placekeeper.plugins.kindle.legacy_catalog import invalidate
from .catalog import books, discover
from .client import pairing_finish, pairing_start

FILE = 'readest-device.json'


def disconnect(root):
    invalidate(root)
    (root/'reader-books.json').unlink(missing_ok=True)


def setup(root, pending, action, data):
    if action == 'start':
        return pairing_start(root/FILE, pending)
    pairing_finish(root/FILE, pending)
    return {'connected': True}


PLUGIN = Plugin('readest', 'Readest', (FILE,),
    'Link your cloud library to supply matching EPUBs.',
    actions=({'id': 'start', 'label': 'Get Readest code'},
             {'id': 'finish', 'label': 'Check approval', 'after': 'start'}),
    setup=setup, disconnect=disconnect, books=books, discover=discover)
