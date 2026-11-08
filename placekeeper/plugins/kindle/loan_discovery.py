"""Find shared titles in Libby, on the X4, and in the Kindle library."""
from collections import Counter, defaultdict
from contextlib import closing
from hashlib import md5
import json
import re
import unicodedata

import httpx

from placekeeper.errors import MissingProgress
from placekeeper.plugins.crosspoint.client import read_progress
from placekeeper.plugins.libby.client import Libby
from placekeeper.plugins.transport import json_reader


def words(value):
    if not isinstance(value, str):
        return []
    return re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', value).casefold())


def metadata_keys(title, authors):
    if not isinstance(title, str):
        return set()
    titles = {''.join(words(part)) for part in
              (title, title.split(':')[-1], title.split(' (')[0])} - {''}
    names = {tuple(sorted(words(author))) for author in authors} - {()}
    return {(key, name) for key in titles for name in names}


class MetadataIndex:
    def __init__(self, records, authors):
        self.records = list(records)
        self.index = defaultdict(list)
        for index, record in enumerate(self.records):
            for key in metadata_keys(record.get('title'), authors(record)):
                self.index[key].append(index)

    def find(self, title, authors):
        indices = {index for key in metadata_keys(title, authors) for index in self.index.get(key, ())}
        return [self.records[index] for index in sorted(indices)]


def loan_authors(loan):
    return [loan.get('firstCreatorName'), loan.get('creator'),
            *(creator.get('name') for creator in loan.get('creators', []) if isinstance(creator, dict))]


def x4_loans(root, source):
    with closing(Libby(root/'libby-device.json')) as libby:
        loans = libby.audiobook_loans()
    if not loans:
        return [], None
    books = source.books()
    credentials = json.loads((root/'crosspoint-auth.json').read_text())
    headers = {'x-auth-user': credentials['username'], 'x-auth-key': credentials['auth_key']}
    selected = {}
    index = MetadataIndex(books, lambda book: [book.get('author')])
    title_counts = Counter(book['title'] for book in books)
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        get = json_reader(client, missing_status=404)
        for loan in loans:
            possible = index.find(loan.get('title'), loan_authors(loan))
            if len(possible) != 1:
                continue
            book = possible[0]
            filename = book['title'] + '.epub'
            if title_counts[book['title']] != 1:
                continue
            if book['id'] in selected:
                continue
            document = md5(filename.encode()).hexdigest()
            try:
                progress = read_progress(get, headers, document)
            except MissingProgress:
                continue
            if progress.get('document') == document and progress.get('progress'):
                selected[book['id']] = book
    return list(selected.values()), books


def kindle_matches(x4_books, documents):
    if not x4_books:
        return {}
    pairs = {}
    index = MetadataIndex(documents.values(), lambda doc: doc.get('authors', []))
    for book in x4_books:
        editions = index.find(book['title'], [book.get('author')])
        personal = [doc for doc in editions if doc.get('kind', 'PDOC') == 'PDOC']
        preferred = personal or editions
        if len(preferred) == 1:
            doc = preferred[0]
            pairs.setdefault(doc['document'], []).append(book)
    return {doc: books[0] for doc, books in pairs.items() if len(books) == 1}
