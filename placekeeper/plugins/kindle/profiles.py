"""Verify saved Kindle/Readest edition maps before they are used."""
import hashlib
import json
from pathlib import Path
import re

from placekeeper.mapping.text import Mapper
from placekeeper.storage import digest


def load_profile(path, *, expected_sha256=None):
    path = Path(path).resolve()
    raw = path.read_bytes()
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('Map fingerprint changed')
    profile = json.loads(raw)
    if not re.fullmatch(r'[A-Z0-9]{10,64}', profile['document']):
        raise ValueError('Invalid Kindle document identifier')
    if not re.fullmatch(r'[a-f0-9]{32}', profile['crosspoint_document']):
        raise ValueError('Invalid CrossPoint document identifier')
    files = {}
    store_book = profile.get('schema_version') == 3
    names = ('epub', 'chapters') if store_book else ('epub', 'content', 'locations')
    for name in names:
        record = profile['files'][name]
        # Existing profiles store paths relative to the project, one level above
        # their private directory. Absolute paths also support moving a bundle.
        source = Path(record['path'])
        if not source.is_absolute():
            source = path.parent.parent/source
        if hashlib.sha256(source.read_bytes()).hexdigest() != record['sha256']:
            raise ValueError(f'{name} fingerprint differs from the validated book map')
        files[name] = source
    if store_book:
        from .store_mapping import StoreMapper
        if (profile.get('kind') != 'EBOK'
                or hashlib.md5(profile['x4_filename'].encode()).hexdigest() != profile['crosspoint_document']):
            raise ValueError('Store profile has an unsupported identity')
        return profile, StoreMapper(files['epub'], json.loads(files['chapters'].read_text())), digest(profile)
    mapper = Mapper(**files)
    if profile.get('schema_version') == 2:
        if (profile.get('status') != 'ready_for_dry_run'
                or profile.get('automatic_progress_writes_enabled') is not False
                or profile.get('bridge',{}).get('model') != 'legacy_sidecar_location_bucket_150'
                or hashlib.md5(profile['x4_filename'].encode()).hexdigest() != profile['crosspoint_document']):
            raise ValueError('Automatic profile has an unsupported identity or sync policy')
        if expected_sha256 is None:
            from placekeeper.plugins.kindle.onboarding import evaluate_indexes, validate_map
            validation = {'text_match': evaluate_indexes(mapper.epub, mapper.kfx),
                          'round_trip': validate_map(mapper)}
        else:
            # The discovery registry pins the profile and its saved validation.
            # File fingerprints above still protect the underlying inputs.
            validation = profile['validation']
        if validation['text_match'].get('accepted') is not True:
            raise ValueError('Automatic profile text alignment failed')
        if validation['round_trip'].get('accepted') is not True:
            raise ValueError('Automatic profile coordinate checks failed')
        return profile, mapper, digest(profile)
    highlight = profile['highlight_check']
    handoff = profile['physical_handoff_check']
    actual = mapper.from_xpointer(highlight['predicted']['xpointer'])
    low, high = actual['sidecar_range']
    if not highlight.get('passed') or not low <= highlight['observed_sidecar_start'] <= high:
        raise ValueError('Highlight calibration check failed')
    actual = mapper.from_xpointer(handoff['predicted']['xpointer'])
    if not handoff.get('passed') or actual['suggested_forward_position'] != handoff['observed_successful_sidecar_write']:
        raise ValueError('Physical handoff calibration check failed')
    return profile, mapper, digest(profile)
