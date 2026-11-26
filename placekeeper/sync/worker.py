"""Internal web job worker; the caller owns the manual-job lock.

Short-lived processes keep large book indexes out of the web process.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from placekeeper.errors import ReadFailure
from placekeeper.storage import ROOT


def run_worker(root, job, notify, *, dry_run=False, timeout=900):
    command = [sys.executable, '-m', __name__, job, '--data-root', str(root)]
    if dry_run:
        command.append('--dry-run')
    with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, start_new_session=True) as process:
        expired = threading.Event()

        def stop():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        def expire():
            expired.set()
            stop()

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        result = None
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    raise ReadFailure('The job returned an invalid response.') from None
                if not isinstance(event, dict):
                    raise ReadFailure('The job returned an invalid response.')
                if 'phase' in event:
                    notify(event['phase'])
                elif 'result' in event:
                    result = event['result']
            code = process.wait()
            if expired.is_set():
                raise ReadFailure('The job timed out. Check the last sync before retrying.')
            expected = dict if job == 'discovery' else list
            if code or not isinstance(result, expected):
                raise ReadFailure('The job could not finish. Check the connection and try again.')
            return result
        finally:
            timer.cancel()
            timer.join()
            if process.poll() is None:
                stop()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job', choices=('progress', 'discovery'))
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.dry_run and args.job != 'progress':
        parser.error('Preview is only available for progress jobs')
    from placekeeper.plugins.registry import installed
    from placekeeper.sync.job import refresh_libraries, run_books

    def emit(**event):
        print(json.dumps(event), flush=True)

    plugins = {key: plugin for key, plugin in installed().items() if plugin.connected(args.data_root)}
    result = (refresh_libraries(args.data_root, plugins) if args.job == 'discovery'
              else run_books(args.data_root, plugins, lambda phase: emit(phase=phase), dry_run=args.dry_run))
    emit(result=result)


if __name__ == '__main__':
    main()
