"""Project paths and private state storage."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def default_data_root():
    override = os.environ.get('PLACEKEEPER_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    legacy = ROOT / '.private'
    if legacy.exists() or legacy.is_symlink():
        return legacy
    return Path.home() / '.config' / 'placekeeper'


def private_json(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


@contextmanager
def state_lock(path):
    # Supported on Linux (Pi) and macOS. Lock a separate inode so atomic state
    # replacement never permits a second poller to race this process.
    import fcntl
    fd = os.open(str(path)+'.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another loop is using this state file') from None
        yield
    finally:
        os.close(fd)


def onboarding_lock(data_root):
    directory = Path(data_root) / 'onboarding'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return state_lock(directory / 'state.json')
