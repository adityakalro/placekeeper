"""Libby plugin registration and device setup."""
import time

from placekeeper.plugins.contracts import Plugin
from .client import Libby
from .reader import open_reader, reader_session

FILE = 'libby-device.json'


def setup(root, pending, action, data):
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
    setup=setup, reader=open_reader, reader_session=reader_session)
