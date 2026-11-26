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


def run_worker(root, job, notify, *, dry_run=False, timeout=900, book_id=None):
    command = [sys.executable, '-m', __name__, job, '--data-root', str(root)]
    if job == 'email':
        if not isinstance(book_id,str) or len(book_id)!=32 or any(c not in '0123456789abcdef' for c in book_id):
            raise ValueError('Unknown EPUB.')
        command.extend(['--book-id',book_id])
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
                raise ReadFailure('Email delivery timed out. Check Kindle before retrying; an uncertain send is protected against duplicates.' if job=='email' else 'The job timed out. Check the last sync before retrying.')
            expected = list if job == 'progress' else dict
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
    parser.add_argument('job', choices=('progress', 'discovery', 'email'))
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--book-id')
    args = parser.parse_args()
    if args.dry_run and args.job != 'progress':
        parser.error('Preview is only available for progress jobs')
    def emit(**event):
        print(json.dumps(event), flush=True)

    if args.job == 'email':
        from placekeeper.plugins.kindle.email_delivery import send_book
        try:result=send_book(args.data_root,args.book_id)
        except ValueError as error:result={'error':str(error)}
        emit(result=result)
        return

    from placekeeper.plugins.registry import installed
    from placekeeper.sync.job import refresh_libraries, run_books
    plugins = {key: plugin for key, plugin in installed().items() if plugin.connected(args.data_root)}
    result = (refresh_libraries(args.data_root, plugins) if args.job == 'discovery'
              else run_books(args.data_root, plugins, lambda phase: emit(phase=phase), dry_run=args.dry_run))
    emit(result=result)


if __name__ == '__main__':
    main()
