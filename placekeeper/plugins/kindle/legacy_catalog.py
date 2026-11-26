"""Compatibility bridge for existing Kindle/Readest book maps and receipts."""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
from hashlib import sha256
import subprocess
import sys

from placekeeper.errors import ReadFailure
from placekeeper.plugins.contracts import Book
from placekeeper.storage import ROOT


def invalidate(root):
    for name in ('state.json', 'ready-profiles.json', 'last-run.json'):
        (root/'onboarding'/name).unlink(missing_ok=True)


def discover(root, connected):
    if 'kindle' not in connected:
        return {}
    options = ['--loan-root', str(root)] if {'crosspoint', 'libby'} <= connected else []
    result = subprocess.run([sys.executable, '-m', 'placekeeper.plugins.kindle.onboarding',
        '--root', str(root/'onboarding'), '--amazon-auth', str(root/'amazon-kindle-download-device.json'),
        '--library-root', str(root), '--kfxlib-parent', str(root/'vendor/python'), *options],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900, cwd=ROOT)
    if result.returncode:
        raise ReadFailure('Could not refresh Kindle and EPUB book matches.')
    report = json.loads((root/'onboarding/last-run.json').read_text())
    counts = dict(Counter(event['status'] for event in report['events']))
    if report.get('loan_check_failed'):
        counts['loan_check_failed'] = 1
    if 'libby' in connected:
        from placekeeper.plugins.libby.validation import validate_books
        registry = json.loads((root/'onboarding/ready-profiles.json').read_text())
        profiles = [json.loads(Path(entry['path']).read_text()) for entry in registry['profiles']]
        report.setdefault('matches', []).extend(validate_books(root, profiles))
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
            raw = path.read_bytes()
            if sha256(raw).hexdigest() != item['sha256']:
                raise ValueError('Map fingerprint changed')
            profile = json.loads(raw)
            document = profile['document']
            epub = Path(profile['files']['epub']['path'])
            if not epub.is_absolute():
                epub = path.resolve().parent.parent / epub
            title = profile.get('title') or profile.get('x4_filename', epub.name).removesuffix('.epub')
            kind = profile.get('kind', 'PDOC')
            names = ('epub', 'chapters') if profile.get('schema_version') == 3 else ('epub', 'content', 'locations')
            edition = ":".join(profile['files'][name]['sha256'] for name in names)
            yield Book('kindle:' + document, title, edition,
                       {'kindle': document, 'kindle_kind': kind, 'crosspoint': profile['crosspoint_document']},
                       epub, initial_state=lambda doc=document: legacy_state(root/'manual-sync'/doc),
                       load_mapper=lambda p=path, sha=item['sha256']: load_profile(p, expected_sha256=sha)[1])
        except (ValueError, OSError, KeyError):
            # The job reports this book and continues with other validated maps.
            yield ReadFailure('Could not validate a Kindle book map.')
