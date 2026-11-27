# Upgrade an existing installation

An upgrade keeps the same data directory and account grants. There is no need to
relink Kindle, CrossPoint, Libby or Readest, recapture positions, or rebuild valid
maps. Legacy `.private` directories and symlinks remain supported; the upgrader
does not move them into a different user's home directory.

## One-time bootstrap from an older commit

Older commits do not contain `upgrade.sh`. Fetch the upgrade runner without
replacing the running checkout first:

```sh
cd /path/to/placekeeper
git fetch origin
placekeeper_upgrade_runner=$(mktemp)
git show origin/main:deploy/upgrade.py > "$placekeeper_upgrade_runner"
```

Then choose **one** of the following commands. Run it as the owner of a home or
Termux installation. For a system-wide install, run it as an administrator able
to manage the existing service, checkout and private data.

Linux/DietPi system service (substitute your existing paths and unit name):

```sh
python3 "$placekeeper_upgrade_runner" --checkout "$PWD" --no-fetch \
  --service placekeeper-web.service --data-root /var/lib/placekeeper
```

A user systemd service:

```sh
python3 "$placekeeper_upgrade_runner" --checkout "$PWD" --no-fetch \
  --service placekeeper-web.service --user-service
```

Termux with the `termux-services` supervisor:

```sh
python3 "$placekeeper_upgrade_runner" --checkout "$PWD" --no-fetch \
  --termux-service placekeeper
```

A manually started installation: stop the existing Python process first, then:

```sh
python3 "$placekeeper_upgrade_runner" --checkout "$PWD" --no-fetch
```

Remove the bootstrap file afterward:

```sh
rm "$placekeeper_upgrade_runner"
```

Use `--data-root` if the service uses a custom data directory not selected by
`PLACEKEEPER_DATA_DIR` or the checkout's `.private` path. For home installations
the fallback is `~/.config/placekeeper` belonging to the user running the command.
The upgrader never invokes sudo for you.

## Subsequent upgrades

Once this version is installed, run the included command from the checkout:

```sh
bash upgrade.sh --service placekeeper-web.service --data-root /var/lib/placekeeper
# Termux:
bash upgrade.sh --termux-service placekeeper
# Stopped manual installation:
bash upgrade.sh
```

The same `--user-service`, `--data-root`, and `--backup-dir` options work here.
`--ref` selects a different target commit or branch; it must be a fast-forward
from the installed commit and include the safe upgrade installer (older targets
require the manual procedure). `--no-fetch` uses an already fetched target for offline
upgrades. Python 3.13 or newer is required before starting.

## What the command does

1. Rejects local edits/untracked files, divergent history, concurrent upgrades,
   insufficient backup space, and unsupported external data symlinks. Ignored
   private files do not make a checkout dirty.
2. Waits up to five minutes for an active web job (including email actions in
   newer versions). An older server may not expose an email-busy flag, so finish
   any Send to Kindle action before upgrading. Stops the selected service only
   after its job is idle. A stopped service stays stopped after the upgrade.
3. Makes an owner-only backup of the complete data directory outside the checkout
   and saves the old virtual environment. Saved file modes and service ownership
   are preserved. The default parent is `.placekeeper-upgrades` beside the checkout;
   `--backup-dir` can choose a different private directory outside the checkout
   and data root. Backups contain credentials: keep them private and out of Git.
4. Fast-forwards the checkout, creates a fresh environment with `install.sh`, and
   runs an offline startup/state check. The installer detects Linux versus Termux. OS packages are not upgraded inside
   this transaction; install any missing system prerequisites separately before
   retrying, so an application rollback cannot leave a replaced system Python.
5. Restarts an originally active service and checks `/api/status`. Existing health
   warnings are allowed; a provider warning does not make the upgrade fail.
6. If installation or startup fails, restores the previous code, environment and
   saved data, then restarts the old service. The backup contains `upgrade.json`
   with the previous/target commits and outcome for recovery.

The existing service unit, allowed hosts/networks, credentials, schedules, book
statuses/removals, progress journals, maps, uploaded EPUBs and email-delivery
ledger are retained. The command does not install or overwrite service units,
register devices, send email, or run a progress sync. A restarted service resumes
its existing schedules, so ordinary scheduled discovery may run afterward.

Stop and disable the obsolete `placekeeper-onboarding.service` or
`book-sync-onboarding.service` if it is active before using the web scheduler;
these independent discovery loops are not compatible with an upgrade in progress.
See [deployment instructions](deployment.md) for the transition from Book Sync or
moving data to a different machine. No automatic data relocation is attempted.

## What changes for older installations

- Readest is optional. Its saved grants, downloaded EPUBs and existing maps keep
  working. You can start uploading EPUBs to the local library without relinking.
- New local-library filenames include an edition suffix. Do not rename existing
  X4 books as part of upgrading. A newly downloaded local-library copy has its own
  filename identity; open it and Sync Progress once before mapping it.
- Existing profiles without an explicit title remain readable. Newly generated
  profiles keep the actual title separately from their X4 download filename.
- Send to Kindle email starts unconfigured and sends only after its button is
  pressed. Existing email credentials and duplicate-protection records survive
  upgrades. An uncertain send is not resent automatically after a restart.
- New optional settings take their documented defaults; existing schedule choices
  are not turned on or reset by the upgrade runner.

Verify Setup and the book list, then use a dry run before applying progress:

```sh
.venv/bin/python -m placekeeper.sync.preview --data-root /your/existing/data
```

Run that command as the service account if it owns the data. It reads provider
positions but does not change them.

## Manual installs and recovery

The automatic command requires a Git checkout. For an archive/copy deployment,
stop all Placekeeper processes, back up the complete data directory and old code
and environment, replace the application files, run `install.sh` with the same
`PLACEKEEPER_DATA_DIR`, restore the service user's ownership of private files,
and start the existing service. Keep the old copies until verified.

A successful upgrade leaves the backup available. For an immediate manual
rollback, stop the service, use the previous commit in `upgrade.json`, put the
saved `venv` back at the checkout's `.venv` path, and restore the saved `data`
to the recorded data root with its original ownership before restarting. Keep
copies of the newer data/environment rather than deleting them. Do not restore
an old data snapshot after newer syncs or email sends without reconciling their
journals and delivery ledger; that can lose duplicate-protection receipts.

If an automatic rollback itself cannot complete (for example, storage or service
control fails), leave the service stopped and recover using those same recorded
paths and backups. Do not rerun installation against the only copy of your data.
