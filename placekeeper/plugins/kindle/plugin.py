"""Kindle plugin registration and browser setup."""
import json
import time
from urllib.parse import parse_qs, urlsplit

from placekeeper.plugins.contracts import Plugin
from placekeeper.storage import private_json
from . import registration as kindle
from .reader import open_reader
from .legacy_catalog import books, discover, invalidate

FILES = ('amazon-device.json', 'amazon-kindle-download-device.json')


def start(root, pending, data):
    if any((root / file).exists() for file in FILES):
        raise ValueError('Kindle setup is incomplete. Disconnect it before reconnecting.')
    if pending.exists() and json.loads(pending.read_text()).get('phase') == 'registering':
        raise ValueError('The earlier registration outcome is uncertain. Check Amazon’s registered devices before starting over.')
    prepared = kindle.plan()
    url, client_id, verifier = kindle.login_details(prepared)
    private_json(pending, {'plan': prepared, 'client_id': client_id, 'verifier': verifier,
                           'expires': time.time()+600, 'phase': 'sign_in'})
    return {'login_url': url, 'device_name': prepared['device_name']}


def finish(root, pending, data):
    saved = json.loads(pending.read_text())
    if saved['expires'] < time.time() or saved['phase'] != 'sign_in':
        raise ValueError('This sign-in expired or was already submitted. Start a new connection.')
    callback = urlsplit(str(data.get('completion_url', '')).strip())
    if callback.scheme != 'https' or callback.netloc != 'www.amazon.com' or callback.path != '/ap/maplanding':
        raise ValueError('Paste the full Amazon completion-page address.')
    fields = parse_qs(callback.query)
    codes = fields.get('openid.oa2.authorization_code', [])
    if len(codes) != 1 or not codes[0]:
        raise ValueError('The completion address has no authorization code. Finish Amazon sign-in first.')
    authorization = {'client_id': saved['client_id'], 'authorization_code': codes[0],
                     'code_verifier': saved['verifier'], 'code_algorithm': 'SHA-256',
                     'client_domain': 'DeviceLegacy'}
    saved['phase'] = 'registering'
    private_json(pending, saved)
    destination = root / 'amazon-kindle-download-device.json'
    kindle.register(saved['plan'], authorization=authorization, destination=destination, data_root=root)
    credentials = json.loads(destination.read_text())
    private_json(root / 'amazon-device.json', credentials)
    pending.unlink(missing_ok=True)
    return {'connected': True}


def setup(root, pending, action, data):
    return {'start': start, 'finish': finish}[action](root, pending, data)


PLUGIN = Plugin('kindle', 'Kindle', FILES,
    'Sign in to link your Kindle library and reading progress.',
    actions=({'id': 'start', 'label': 'Start Amazon sign-in',
              'consent': 'Allow Placekeeper to register a Kindle device and save its access.'},
             {'id': 'finish', 'label': 'Complete connection', 'after': 'start',
              'fields': [{'name': 'completion_url', 'label': 'Paste the Amazon completion-page address', 'type': 'url'}]}),
    setup=setup, reader=open_reader, books=books, discover=discover, disconnect=invalidate, catalog_requires=('readest',))
