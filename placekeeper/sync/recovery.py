"""Inspect uncertain progress writes and release a verified journal entry."""
import argparse
import json
from pathlib import Path
import re

from placekeeper.storage import ROOT, private_json, state_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=ROOT/'.private')
    parser.add_argument('--clear', metavar='JOURNAL_ID',
                        help='Clear a pending write after confirming it did not land')
    args = parser.parse_args()
    root = args.data_root.resolve()
    if args.clear and not re.fullmatch(r'[0-9a-f]{64}', args.clear):
        parser.error('Use the journal ID shown by the recovery list.')
    if not root.exists():
        parser.error('The Placekeeper data directory does not exist.')

    with state_lock(root/'manual-sync-job'):
        directory = root/'progress'
        if args.clear:
            path = directory/(args.clear + '.json')
            if not path.is_file():
                parser.error('No journal has that ID.')
            state = json.loads(path.read_text())
            if 'pending' not in state:
                parser.error('That journal has no pending write.')
            state.pop('pending')
            private_json(path, state)
            print('Pending write cleared. Run Sync now to recheck the readers.')
            return

        entries = []
        for path in sorted(directory.glob('*.json')):
            state = json.loads(path.read_text())
            if pending := state.get('pending'):
                entries.append({'id': path.stem, 'title': state.get('title'),
                                'destination': pending['device'], 'before': pending['before']['position'],
                                'target': pending['target']})
        print(json.dumps(entries, indent=2))


if __name__ == '__main__':
    main()
