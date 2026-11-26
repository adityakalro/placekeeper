"""Local reading states and conservative automatic sync eligibility."""
import json
import math

from placekeeper.storage import private_json

MODES = ('automatic', 'reading', 'not_started', 'finished', 'paused')
LABELS = {'not_started': 'Not started', 'finished': 'Finished', 'paused': 'Paused'}


def load(root):
    path = root/'book-status.json'
    return json.loads(path.read_text()) if path.exists() else {}


def save_mode(root, key, mode):
    books = load(root)
    if not isinstance(key, str) or key not in books or mode not in MODES:
        raise ValueError('Choose a known book and a valid reading status.')
    if books[key].get('removed'):
        raise ValueError('Restore this book before changing its status.')
    books[key]['mode'] = mode
    private_json(root/'book-status.json', books)


def save_removed(root, key, removed):
    books = load(root)
    if not isinstance(key, str) or key not in books or type(removed) is not bool:
        raise ValueError('Choose a known book to remove or restore.')
    books[key]['removed'] = removed
    private_json(root/'book-status.json', books)


def automatic_status(positions):
    fractions = [p.fraction for p in positions.values()]
    known = [value for value in fractions if type(value) in (int, float)
             and math.isfinite(value) and 0 <= value <= 1]
    if known and max(known) >= .995:
        return 'finished'
    if known and len(known) == len(fractions) and max(known) <= .001:
        return 'not_started'
    return 'reading'


def skipped(status):
    return {'status': 'paused', 'reading_status': status, 'writes': 0,
            'message': LABELS[status]+'. Progress sync is paused. Choose Reading to resume.'}
