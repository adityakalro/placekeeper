"""Compatibility bridge for existing Kindle/Readest book maps and receipts."""
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

from placekeeper.errors import ReadFailure
from placekeeper.plugins.contracts import Book
from placekeeper.storage import ROOT


def invalidate(root):
    for name in ('state.json', 'ready-profiles.json', 'last-run.json'):
        (root/'onboarding'/name).unlink(missing_ok=True)


def discover(root, connected):
    if not {'kindle', 'readest'} <= connected:
        return {}
    options = ['--loan-root', str(root)] if {'crosspoint', 'libby'} <= connected else []
    result = subprocess.run([sys.executable, '-m', 'placekeeper.plugins.kindle.onboarding',
        '--root', str(root/'onboarding'), '--amazon-auth', str(root/'amazon-kindle-download-device.json'),
        '--readest-auth', str(root/'readest-device.json'), '--kfxlib-parent', str(root/'vendor/python'), *options],
        capture_output=True, timeout=900, cwd=ROOT)
    if result.returncode:
        raise ReadFailure('Could not refresh Kindle and Readest book matches.')
    report = json.loads((root/'onboarding/last-run.json').read_text())
    counts = dict(Counter(event['status'] for event in report['events']))
    if 'matches' in report:
        counts['matches'] = report['matches']
    return counts


def legacy_state(directory):
    aliases = {'Kindle': 'kindle', 'X4': 'crosspoint', 'Libby': 'libby'}
    state = {'receipts': {}}
    prior = directory/'three-way-state.json'
    if prior.exists():
        saved = json.loads(prior.read_text())
        state['receipts'] = {aliases[key]: value for key, value in saved['receipts'].items()}
        if saved.get('pending'):
            pending = saved['pending']
            state['pending'] = {**pending, 'device': aliases[pending['device']]}
    prior = directory/'result.json'
    if prior.exists():
        saved = json.loads(prior.read_text())
        result = saved.get('result', {})
        if result.get('status') == 'synced' and result.get('direction') == 'crosspoint_to_kindle':
            stamp = datetime.fromisoformat(saved['finished_at']).timestamp()
            if stamp > (state['receipts'].get('kindle', {}).get('stamp') or 0):
                state['receipts']['kindle'] = {'position': saved['readback']['kindle_position'], 'stamp': stamp}
    return state


def books(root):
    from .profiles import load_profile

    registry = root/'onboarding/ready-profiles.json'
    if not registry.exists():
        return
    for item in json.loads(registry.read_text())['profiles']:
        path = Path(item['path'])
        try:
            if sha256(path.read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('Map fingerprint changed')
            profile, mapper, _ = load_profile(path)
            document = profile['document']
            epub = Path(profile['files']['epub']['path'])
            if not epub.is_absolute():
                epub = path.resolve().parent.parent / epub
            title = profile.get('x4_filename', epub.name).removesuffix('.epub')
            kind = profile.get('kind', 'PDOC')
            names = ('epub', 'chapters') if kind == 'EBOK' else ('epub', 'content', 'locations')
            edition = ":".join(profile['files'][name]['sha256'] for name in names)
            yield Book('kindle:' + document, title, edition,
                       {'kindle': document, 'kindle_kind': kind, 'crosspoint': profile['crosspoint_document']},
                       epub, mapper,
                       lambda doc=document: legacy_state(root/'manual-sync'/doc))
        except (ValueError, OSError, KeyError):
            # The job reports this book and continues with other validated maps.
            yield ReadFailure('Could not validate a Kindle book map.')
