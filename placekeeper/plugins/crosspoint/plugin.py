"""CrossPoint plugin registration and account setup."""
from placekeeper.plugins.contracts import Plugin
from placekeeper.storage import private_json
from .client import SERVER, authenticate
from .reader import open_reader, reader_session

FILE = 'crosspoint-auth.json'


def setup(root, pending, action, data):
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))
    if not username or not password:
        raise ValueError('Enter the sync username and password used on your X4.')
    key = authenticate(username, password)
    private_json(root/FILE, {'username': username, 'auth_key': key, 'server': SERVER,
                            'purpose': 'User-approved manual reading-progress sync'})
    return {'connected': True}


PLUGIN = Plugin('crosspoint', 'CrossPoint', (FILE,),
    'Use the sync account configured on your X4. Placekeeper saves the sign-in key.',
    actions=({'id': 'start', 'label': 'Connect CrossPoint',
              'fields': [{'name': 'username', 'label': 'Sync username'},
                         {'name': 'password', 'label': 'Sync password', 'type': 'password'}]},),
    setup=setup, reader=open_reader, reader_session=reader_session)
