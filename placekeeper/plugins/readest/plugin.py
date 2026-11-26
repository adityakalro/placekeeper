"""Readest content-provider registration."""
from placekeeper.plugins.contracts import Plugin
from placekeeper.plugins.lazy import deferred

FILE = 'readest-device.json'


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
    setup=setup)
