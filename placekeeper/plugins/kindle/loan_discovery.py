"""Find shared titles in Libby, on the X4, and in the Kindle library."""
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


def matches(title, authors, other_title, other_authors):
    def title_keys(value):
        if not isinstance(value, str):
            return set()
        return {''.join(words(part)) for part in
                (value, value.split(':')[-1], value.split(' (')[0])} - {''}

    title_match = title_keys(title) & title_keys(other_title)
    names = {tuple(sorted(words(author))) for author in authors} - {()}
    return bool(title_match
                and any(tuple(sorted(words(author))) in names for author in other_authors))


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
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        get = json_reader(client, missing_status=404)
        for loan in loans:
            possible = [book for book in books if matches(loan.get('title'), loan_authors(loan),
                                                         book['title'], [book.get('author')])]
            if len(possible) != 1:
                continue
            book = possible[0]
            filename = book['title'] + '.epub'
            if sum(item['title'] == book['title'] for item in books) != 1:
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
    pairs = {}
    for book in x4_books:
        editions = [doc for doc in documents.values() if matches(
            book['title'], [book.get('author')], doc.get('title'), doc.get('authors', []))]
        personal = [doc for doc in editions if doc.get('kind', 'PDOC') == 'PDOC']
        preferred = personal or editions
        if len(preferred) == 1:
            doc = preferred[0]
            pairs.setdefault(doc['document'], []).append(book)
    return {doc: books[0] for doc, books in pairs.items() if len(books) == 1}
