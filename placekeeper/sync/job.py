"""Independent catalog discovery and progress jobs for the plugin registry."""
from collections import Counter
from contextlib import ExitStack
from dataclasses import replace

from placekeeper.errors import ReadFailure
from placekeeper.storage import onboarding_lock, private_json
from placekeeper.sync import book_status
from placekeeper.sync.engine import sync_book


def refresh_libraries(root, plugins):
    counts = Counter()
    matches = None
    for key, plugin in plugins.items():
        if plugin.discover is None or not all(k in plugins for k in plugin.catalog_requires):
            continue
        try:
            result = dict(plugin.discover(root, set(plugins)))
            if 'matches' in result:
                matches = (matches or []) + result.pop('matches')
            counts.update(result)
        except Exception:
            counts['discovery_error'] += 1
    return dict(counts) if matches is None else {**counts, 'matches': matches}


def run_books(root, plugins, notify, *, dry_run=False):
    with onboarding_lock(root), ExitStack() as stack:
        readers = {
            key: replace(plugin, reader=stack.enter_context(plugin.reader_session(root)))
            if plugin.reader_session is not None else plugin
            for key, plugin in plugins.items()
        }
        return run_catalogs(root, plugins, readers, notify, dry_run)


def run_catalogs(root, plugins, readers, notify, dry_run):
    results = []
    statuses = book_status.load(root)
    seen = set()
    for key, plugin in plugins.items():
        if not plugin.catalog_ready(plugins):
            continue
        try:
            for book in plugin.books(root):
                if isinstance(book, ReadFailure):
                    results.append(error_result(plugin.label, str(book)))
                    continue
                if book.key in seen:
                    continue
                seen.add(book.key)
                notify('Syncing ' + book.title + '…')
                try:
                    result = sync_book(root, book, readers, dry_run=dry_run,
                                       mode=statuses.get(book.key, {}).get('mode', 'automatic'))
                except Exception as error:
                    message = str(error) if isinstance(error, ReadFailure) else 'Could not open the book or a connected reader.'
                    result = error_result(book.title, message)
                finally:
                    book.release()
                if result.get('source') in plugins:
                    source = result['source']
                    result['message'] = result['message'].replace(source + ' →', plugins[source].label + ' →', 1)
                if not dry_run:
                    previous = statuses.get(book.key, {})
                    statuses[book.key] = {'key': book.key, 'title': book.title,
                        'mode': previous.get('mode', 'automatic'),
                        'reading_status': result.get('reading_status',
                            'reading' if result['status'] not in ('error', 'unconfirmed') else previous.get('reading_status', 'unknown'))}
                    private_json(root/'book-status.json', statuses)
                results.append({'key': book.key, 'title': book.title, **result})
                del book
        except Exception:
            results.append(error_result(plugin.label, 'Could not load the book catalog.'))
    return results


def error_result(title, message):
    return {'title': title, 'status': 'error', 'writes': 0, 'message': message}
