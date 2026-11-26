"""Discover EPUBs shared by CrossPoint and Libby without a Kindle map."""
from contextlib import closing
from hashlib import md5, sha256
import json
from pathlib import Path

from placekeeper.errors import ReadFailure
from placekeeper.plugins.contracts import Book
from placekeeper.plugins.kindle.loan_discovery import MetadataIndex, active_loans, loan_authors, x4_loans
from placekeeper.storage import file_sha256, private_json
from placekeeper.plugins.library.source import LibrarySource
from .storage import book_path


def discover(root, connected):
    if not {'crosspoint', 'libby'} <= connected:
        return {}
    registry_path = root/'reader-books.json'
    saved = json.loads(registry_path.read_text()) if registry_path.exists() else {'books': []}
    previous = {entry['id']: entry for entry in saved['books']}
    with closing(LibrarySource(root)) as source:
        catalog = source.books()
        loans = active_loans(root)
        shared = x4_loans(root, catalog, loans)
        index = MetadataIndex(loans, loan_authors)
        entries = []
        for book in shared:
            matches = index.find(book['title'], [book.get('author')])
            if len(matches) != 1:
                continue
            epub = book_path(root,book['id']) if book.get('local') else root/'reader-books'/(book['id']+'.epub')
            old = previous.get(book['id'], {})
            fingerprint = file_sha256(epub) if epub.is_file() else None
            if fingerprint is None or fingerprint != old.get('sha256'):
                epub = source.download(book, root/'reader-books')
                fingerprint = file_sha256(epub)
            entries.append({'title': book['title'], 'id': book['id'], 'epub': str(epub.resolve()),
                            'sha256': fingerprint,
                            'crosspoint': md5((book.get('filename', book['title']+'.epub')).encode()).hexdigest(),
                            'libby': matches[0]})
    private_json(root/'reader-books.json', {'books': entries})
    return {'matches': [{'title': item['title'], 'status': 'ready',
                         'services': ['crosspoint', 'libby']} for item in entries]}


def books(root):
    registry = root/'reader-books.json'
    if not registry.exists():
        return
    # Kindle maps already include these readers. Avoid a second job for the EPUB.
    mapped = set()
    kindle = root/'onboarding/ready-profiles.json'
    if (root/'amazon-device.json').exists() and kindle.exists():
        for entry in json.loads(kindle.read_text())['profiles']:
            try:
                raw=Path(entry['path']).read_bytes()
                profile = json.loads(raw)
                epub = profile['files']['epub']
                if (sha256(raw).hexdigest() == entry['sha256']
                        and file_sha256(epub['path']) == epub['sha256']):
                    mapped.add(epub['sha256'])
            except (OSError, ValueError, KeyError):
                continue
    for entry in json.loads(registry.read_text())['books']:
        epub = Path(entry['epub'])
        if entry['sha256'] in mapped:
            continue
        if not epub.is_file() or file_sha256(epub) != entry['sha256']:
            yield ReadFailure('The library EPUB changed. Run Find new books again.')
            continue
        yield Book('readest:'+entry['id'], entry['title'], entry['sha256'],
                   {'crosspoint': entry['crosspoint']}, epub)
