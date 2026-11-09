"""Execute plans against an arbitrary set of reader sessions."""
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
from time import time

from placekeeper.errors import MissingProgress, ReadFailure, UnsupportedMapping
from placekeeper.storage import private_json
from placekeeper.sync.planner import plan


def observe(readers, state, pool, mapping_skips):
    pending = state.get('pending') or {}

    def read(key, reader):
        hint = {'need_timestamp': False, 'receipt': state['receipts'].get(key),
                'pending': pending if pending.get('device') == key else None}
        try:
            return reader.read(hint)
        except MissingProgress:
            return None
        except UnsupportedMapping as error:
            return error

    parallel = {key: pool.submit(read, key, reader) for key, reader in readers.items()
                if getattr(reader, 'parallel_reads', False) is True}
    wait(parallel.values())
    positions = {}
    for key, reader in readers.items():
        position = parallel[key].result() if key in parallel else read(key, reader)
        if isinstance(position, UnsupportedMapping):
            mapping_skips[key] = str(position)
        elif position is not None:
            positions[key] = position
    return positions


def receipt(observation):
    return {'position': observation.native, 'stamp': observation.stamp}


def reconcile(state, observations):
    pending = state.get('pending')
    if not pending:
        return
    key = pending['device']
    current = observations.get(key)
    if current is None or current.native == pending['before']['position']:
        raise ReadFailure('An earlier write is unconfirmed. Check the destination and use the recovery command before retrying.')
    if current.native == pending['target']:
        state['receipts'][key] = receipt(current)
    state.pop('pending')


def execute(readers, state, path, dry_run=False, *, skipped=None):
    with ThreadPoolExecutor(max_workers=3) as pool:
        skipped = {} if skipped is None else skipped
        mapping_skips = {}
        result = execute_plan(readers, state, path, dry_run, pool, mapping_skips)
    skipped.update(mapping_skips)
    if skipped:
        result['skipped_readers'] = skipped
        result['message'] += ' ' + ' '.join(skipped.values())
    return result


def execute_plan(readers, state, path, dry_run, pool, mapping_skips):
    writes = 0
    outcomes = {}
    try:
        positions = observe(readers, state, pool, mapping_skips)
        if state.get('pending'):
            reconcile(state, positions)
            if not dry_run:
                private_json(path, state)
        source, targets, message = plan(readers, positions, state['receipts'], state.get('handoff'), mapping_skips)
        if source is not None and not targets and not dry_run and 'handoff' in state:
            state.pop('handoff')
            private_json(path, state)
        if source is None:
            return {'status': 'skipped' if len(positions) < 2 else 'in_sync',
                    'writes': 0, 'message': message}
        if dry_run:
            return {'status': 'preview', 'writes': 0, 'source': source, 'targets': targets,
                    'message': 'Preview from ' + source}
        outcomes.update({key: 'kept' for key in positions
                         if key != source and key not in targets and key not in mapping_skips})
        for key, target in targets.items():
            fresh = observe(readers, state, pool, mapping_skips)
            if fresh != positions:
                return {'status': 'changed', 'writes': writes, 'outcomes': outcomes,
                        'message': 'A reader changed during sync. Try again.'}
            state['handoff'] = {'source': source, 'position': receipt(fresh[source]),
                                'anchor': fresh[source].anchor}
            state['pending'] = {'device': key, 'target': target,
                                'before': receipt(fresh[key]), 'stamp': time()}
            private_json(path, state)
            writes += 1
            readers[key].write(target)
            after = observe(readers, state, pool, mapping_skips)
            if key not in after or after[key].native != target:
                raise ReadFailure(f'{key} saved position could not be verified.')
            state['receipts'][key] = receipt(after[key])
            state.pop('pending')
            private_json(path, state)
            outcomes[key] = 'verified'
            if {k: v for k, v in after.items() if k != key} != {k: v for k, v in fresh.items() if k != key}:
                return {'status': 'changed', 'writes': writes, 'outcomes': outcomes,
                        'message': 'Another reader changed during sync. Try again.'}
            positions = after
        if writes:
            state.pop('handoff', None)
            private_json(path, state)
        verified = [key for key, result in outcomes.items() if result == 'verified']
        return {'status': 'synced' if writes else 'in_sync', 'writes': writes, 'source': source,
                'outcomes': outcomes,
                'message': f'{source} → {", ".join(verified)} verified'
                if writes else 'The selected position required no supported updates.'}
    except Exception as error:
        return {'status': 'unconfirmed' if writes else 'error', 'writes': writes, 'outcomes': outcomes,
                'message': str(error) if isinstance(error, ReadFailure) else 'Could not complete this book’s sync.'}


def sync_book(root, book, plugins, dry_run=False):
    path = Path(root) / 'progress' / (sha256(book.key.encode()).hexdigest() + '.json')
    existing = path.exists()
    if existing:
        state = json.loads(path.read_text())
    else:
        state = book.initial_state() if book.initial_state else {'receipts': {}}
    if state.get('edition', book.edition) != book.edition:
        if state.get('pending'):
            raise ReadFailure('An unconfirmed write belongs to an earlier edition map.')
        state = {'receipts': {}}
    state.update(edition=book.edition, title=book.title)
    if not existing and not dry_run and state.get('pending'):
        private_json(path, state)
    skipped = {}
    with ExitStack() as stack:
        readers = {}
        for key, plugin in plugins.items():
            if plugin.reader is None:
                continue
            try:
                session = stack.enter_context(plugin.reader(root, book))
            except MissingProgress:
                continue
            except UnsupportedMapping as error:
                skipped[key] = str(error)
                continue
            if session is not None:
                readers[key] = session
        return execute(readers, state, path, dry_run, skipped=skipped)
