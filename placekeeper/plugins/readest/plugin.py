"""Readest content-provider registration."""
from placekeeper.plugins.contracts import Plugin
from placekeeper.plugins.lazy import deferred

FILE = 'readest-device.json'


def disconnect(root):
    from placekeeper.plugins.kindle.legacy_catalog import invalidate
    invalidate(root)
    (root/'reader-books.json').unlink(missing_ok=True)


def setup(root, pending, action, data):
    from .client import pairing_finish, pairing_start
    if action == 'start':
        return pairing_start(root/FILE, pending)
    pairing_finish(root/FILE, pending)
    return {'connected': True}


PLUGIN = Plugin('readest', 'Readest', (FILE,),
    'Link your cloud library to supply matching EPUBs.',
    actions=({'id': 'start', 'label': 'Get Readest code'},
             {'id': 'finish', 'label': 'Check approval', 'after': 'start'}),
    setup=setup, disconnect=disconnect, books=deferred(__package__+'.catalog', 'books'),
    discover=deferred(__package__+'.catalog', 'discover'))
