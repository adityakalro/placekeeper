"""Libby plugin registration and device setup."""
import time

from placekeeper.plugins.contracts import Plugin
from placekeeper.plugins.lazy import deferred

FILE = 'libby-device.json'


def setup(root, pending, action, data):
    from .client import Libby
    client = Libby(pending)
    try:
        if action == 'start':
            return client.pairing_start()
        if client.state.get('expires', 0) < time.time():
            raise ValueError('This Libby setup code expired. Get a new code.')
        client.pairing_finish(root/FILE)
        return {'connected': True}
    finally:
        client.close()


PLUGIN = Plugin('libby', 'Libby', (FILE,),
    'Enter the code in Libby under Menu → Copy To Another Device.',
    actions=({'id': 'start', 'label': 'Get Libby code'},
             {'id': 'finish', 'label': 'Check connection', 'after': 'start'}),
    setup=setup, reader=deferred(__package__+'.reader', 'open_reader'),
    reader_session=deferred(__package__+'.reader', 'reader_session'))
