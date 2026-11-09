"""Preview progress changes with the same planner as Sync now."""
import argparse
import json
from pathlib import Path

from placekeeper.storage import default_data_root, state_lock
from placekeeper.sync.job import run_books
from placekeeper.plugins.registry import installed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=default_data_root())
    root = parser.parse_args().data_root.resolve()
    if not root.exists():
        print('[]')
        return
    plugins = {key: plugin for key, plugin in installed().items() if plugin.connected(root)}
    with state_lock(root/'manual-sync-job'):
        print(json.dumps(run_books(root, plugins, lambda _: None, dry_run=True)))


if __name__ == '__main__':
    main()
