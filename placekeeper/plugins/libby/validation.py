"""Check audiobook chapter mappings during discovery without changing progress."""
from contextlib import closing
import json
from pathlib import Path

from placekeeper.errors import MissingProgress, ReadFailure
from placekeeper.mapping.chapters import ChapterMap, chapter_key
from placekeeper.mapping.text import epub_index
from placekeeper.storage import digest, file_sha256, private_json
from .client import Libby


def validate_books(root, profiles):
    results = []
    cache_path = root/'audio-validation.json'
    try:
        cached = json.loads(cache_path.read_text())
        if not isinstance(cached, dict):
            cached = {}
    except (FileNotFoundError, ValueError):
        cached = {}
    with closing(Libby(root/'libby-device.json')) as libby:
        for profile in profiles:
            title = profile['x4_filename'].removesuffix('.epub')
            epub = Path(profile['files']['epub']['path'])
            try:
                loan = libby.match(title, epub)
                if loan is None:
                    continue
                player, _, timing = libby.open_player(loan)
                with closing(player):
                    fingerprint = file_sha256(epub)
                    if fingerprint != profile['files']['epub']['sha256']:
                        raise ValueError('EPUB fingerprint changed')
                    key = digest({'version': 1, 'epub': fingerprint,
                                  'chapters': timing['chapters'], 'duration': timing['duration']})
                    checked = cached.get(key)
                    if type(checked) is not int or checked <= 0:
                        checked = validate_chapters(epub, timing)
                        cached[key] = checked
                        private_json(cache_path, cached)
                status = 'ready'
                try:
                    libby.position(loan)
                except MissingProgress:
                    status = 'open_libby'
                results.append({'title': title, 'status': status, 'chapter_checks': checked})
            except ValueError:
                results.append({'title': title, 'status': 'audio_mapping_required'})
            except (ReadFailure, OSError):
                results.append({'title': title, 'status': 'audio_unavailable'})
    return results


def validate_chapters(epub, timing):
    coordinates = epub_index(epub).coordinates
    mapping = ChapterMap(epub, coordinates, timing)
    checked = 0
    for label, first, last, start, end in mapping.rows:
        if not chapter_key(label).startswith('chapter '):
            continue
        for fraction in (.1, .5, .9):
            sample = int(start+(end-start)*fraction)
            pointer, _ = mapping.to_text(sample)
            _, returned = mapping.to_audio(pointer)
            if returned != label:
                raise ValueError('Chapter round trip failed')
            checked += 1
    if checked == 0:
        raise ValueError('No numbered chapters')
    return checked
