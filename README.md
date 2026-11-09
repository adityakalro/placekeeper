# Placekeeper

Placekeeper helps you pick up where you left off across Send to Kindle documents, an X4 running CrossPoint, and Libby audiobooks. It runs on Linux, including a Raspberry Pi Zero 2 W running DietPi, or on Android through Termux. Open the private web page and click **Sync now** whenever you want to transfer your place.

## Before you start

- A Linux host or an Android device with Termux. You need Git to clone the repository and Python 3.13 or newer to run it.
- An Amazon account with a **Send to Kindle** personal document and the matching EPUB in **Readest Cloud**. Use the same ebook edition in both places.
- At least one sync destination. Use the sync account configured on your **CrossPoint X4**, an active **Libby audiobook loan**, or both.

Each installation uses one household’s accounts. The included Kindle catalog uses Readest to match editions. Additional plugins can provide their own verified catalogs. Kindle positions are approximate to a location bucket, and Libby positions are approximate within matched chapters. Placekeeper can build maps for Send to Kindle documents and supported Kindle Store books. Store maps align numbered chapters and estimate positions within each chapter.

## 1. Install Placekeeper

From a Linux or Termux shell, run:

```sh
git clone https://github.com/adityakalro/placekeeper.git
cd placekeeper
bash install.sh
```

The installer detects Termux or Linux, creates a virtual environment, installs the Python dependencies and pinned KFX parser, and creates a localhost web configuration. Rerunning it preserves existing account data and web settings. On Debian 13 and DietPi, it uses APT for missing system prerequisites. On other Linux distributions, install Git and Python 3.13 or newer with venv and pip through your package manager first. If you need Git to clone on Debian, run `sudo apt install git`. On Termux, run `pkg install git`.

## 2. Start the private web page

```sh
.venv/bin/python -m placekeeper.web.app
```

Open [Setup](http://127.0.0.1:8780/setup) in a browser on the host. If the host is a headless Pi, leave Placekeeper running and open a second terminal **on your computer**. Forward the port over SSH, substituting your own SSH username and Pi address:

```sh
ssh -N -L 127.0.0.1:8780:127.0.0.1:8780 YOUR_USER@YOUR_PI_HOST
```

Keep the tunnel open while using the same [local Setup address](http://127.0.0.1:8780/setup).

### Run in the background and start at boot

- **Linux and DietPi.** Install the included systemd service and run `sudo systemctl enable --now placekeeper-web.service` to start it now and at boot. See the [Linux service guide](docs/deployment.md#2-start-the-service-and-connect-accounts) for service installation and private network access.
- **Termux.** Install `termux-services`, register Placekeeper with its supervisor, and run `sv-enable placekeeper` to run it in the background. Install Termux:Boot and add the startup script to bring it back after an Android reboot. See the [Termux guide](docs/termux.md#run-in-the-background) for the exact commands.

## 3. Connect your accounts

| Service | What to do in **Setup** |
| --- | --- |
| Kindle | Approve device registration and start Amazon sign-in. Sign in on Amazon, then paste the full address of the completion page into Placekeeper and select **Complete connection**. |
| Readest | Select **Get Readest code**, open the approval link, and approve the reader in Readest. Return to Placekeeper and select **Check approval**. |
| CrossPoint | Enter the sync username and password configured on the X4. Approve saving the sign-in key and select **Connect CrossPoint**. |
| Libby | Select **Get Libby code**. In Libby, use **Menu → Copy To Another Device** and enter the code. Return to Placekeeper and select **Check connection**. |

Once Setup says **Ready to sync**, return to the **Sync** page. The credentials are saved with owner-only permissions under `.private/`. **Disconnect** removes a connection from this installation. Manage remote device or reader grants in the provider’s account settings.

## 4. Sync your first book

1. Send the EPUB to Kindle with **Add to your library** enabled. Upload the same edition to **Readest Cloud** so Placekeeper can download it.
2. Open the personal document on Kindle, turn a page, and sync Kindle. If you use an X4, download the EPUB from Readest, keep its `Title.epub` filename, use filename-based document matching, and run **Sync Progress** in the book. If you use Libby, open the matching audiobook loan, listen briefly, and sync your shelf.
3. In Placekeeper, click **Find new books** to discover the book and build its map. A long book can take a minute or more on a small Pi. When discovery finishes, click **Sync now** to transfer progress. Each job shows its own result.
4. On the destination, use **Sync Progress** on X4, **Sync to Furthest Page Read** on Kindle, or **Shelf → Actions → Synchronize Shelf** in Libby. Open the book or audiobook to pick up the saved position.

For future handoffs, sync the device you were using before clicking **Sync now**. Placekeeper compares mapped reading and listening positions and moves readers forward to the furthest position. It suppresses echoes from its own earlier writes. In **Setup**, you can enable automatic progress sync every 30 minutes. It starts off disabled, and the Sync page shows whether it is on. Both **Sync now** and the schedule use existing maps. Use **Find new books** when you add a title.

## How Find new books selects titles

With Libby and CrossPoint connected, Placekeeper starts with active Libby audiobook loans. It matches the title and author to the Readest EPUB catalog and checks for a saved X4 position using the EPUB filename. Then it checks the Kindle library for a matching personal document or Kindle store book. Run **Sync Progress** on the X4 once for a book to make it discoverable. Keep the Readest filename and use filename matching on the X4.

The discovery result lists the titles found on all three services. When both copies map to the same EPUB, the verified personal document takes precedence. For Store editions, Placekeeper downloads chapter and location data using the connected Kindle device and a compatible Amazon voucher. If Amazon denies delivery or the chapter sequences disagree, the Store copy stays out of progress sync. If a mapped book is missing from Libby or the X4, the other two readers can still sync when both have saved positions.

## Using only some services

For the built-in catalog, Kindle and Readest provide the book maps. Connect CrossPoint, Libby, or both. A book can sync between any two connected reading services with saved positions. A missing X4 position or an unread Kindle copy can be left out of an audio handoff. An audiobook loan without saved listening progress leaves Kindle and X4 on their normal ebook sync path. When fewer than two positions are available, the book is skipped.

If a connected service returns a connection or authentication error, Placekeeper reports it for that book. It can still process the other books in the job.

## If a book does not sync

An interrupted write may need a check on the destination. Run `.venv/bin/python -m placekeeper.sync.recovery` on the host using the Placekeeper service account and data directory. If the target landed, run **Sync now** to verify it. If the destination has settled and the write did not land, use `--clear JOURNAL_ID` and run **Sync now**. Placekeeper rechecks the readers before resuming a partial handoff.


- **Waiting for an EPUB in Readest:** Confirm the matching edition is in Readest Cloud and the Kindle copy is in your Send to Kindle library.
- **Missing X4 progress:** Check the filename and matching mode, then open the book on the X4 and select **Sync Progress**.
- **Libby title or chapters could not be matched:** Check that the active loan is the same book and author. Audio sync needs a compatible chapter sequence.
- **Connection failed:** Check **Setup**, your network, and your device’s cloud sync. The providers’ unofficial endpoints may change.

## Onboarding and maps

```sh
.venv/bin/python -m placekeeper.sync.onboarding
```

The page runs this step on demand. A separate `python -m placekeeper.sync.onboarding --watch` mode is available, but do not enable the old timed service alongside the manual page if you want fully on-demand behavior.

Titles narrow candidate EPUBs; metadata alone never establishes a match. Acceptance requires identical normalized reading text, 120 distributed unique context matches in each direction, and at least 95% of 40 coordinate round trips. Quarantined books are retried when the catalog changes. Title metadata with no plausible candidate waits instead of bulk-downloading unrelated documents.

The X4 identity adapter currently assumes the confirmed filename-MD5 mode and Readest downloads named `Title.epub`. Renaming the X4 file or changing its hash mode requires another identity adapter.

Private output:

- `.private/onboarding/ready-profiles.json`: the accepted-profile registry; do not glob old profiles.
- `.private/onboarding/books/<document>/profile.json`: EPUB/KFX fingerprints and validation.
- `.private/onboarding/state.json`: completed library cursor, jobs, retries, and cache fingerprints.
- `.private/manual-sync/`: private before-write snapshots and verification records.
- `.private/web-last-sync.json`: last manual job result.

Automatically generated profiles remain marked `ready_for_dry_run`; this records structural validation, not a per-book physical handoff test. The Sync page can apply handoffs on demand, and enabling automatic progress sync in Setup allows scheduled handoffs.

Library pagination must complete before the cursor is committed. Cache changes trigger rebuilds. Removed or newly ambiguous matches leave the ready registry. Only one manual job runs at a time, and every handoff rechecks the connected readers before writing.

To preview a handoff using the same planner as **Sync now**:

```sh
.venv/bin/python -m placekeeper.sync.preview
```

The preview uses the connected services and mapped books from your installation. The older `placekeeper.sync.dry_run` command is an alias for this preview and accepts the same `--data-root` option.

## Code layout

Service adapters live in `placekeeper/plugins/`. The sync jobs in `placekeeper/sync/` match books and decide which positions to update. Shared text and chapter maps live in `placekeeper/mapping/`, and the web server and templates live in `placekeeper/web/`. Tests and synthetic fixtures are under `tests/`, and systemd units and configuration examples are under `deploy/`.

Add a reader or catalog by installing a plugin package. The [plugin guide](docs/plugins.md) describes the contract and shows how to test a fourth reader.

## Tests and privacy

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The [synthetic test EPUB](tests/fixtures/Placekeeper%20Progress%20Test.epub) contains synthetic test prose. Real books, reading history, credentials, personal host details, and account-specific experiments are excluded. Optional real-book checks require a local profile supplied through `PLACEKEEPER_TEST_PROFILE`.

See [sharing guide](docs/sharing.md) for the publication boundary. The implementation is a personal prototype, using unofficial service endpoints.

Protocol references: [Kindle registration research](https://github.com/mkb79/kindle), [personal-document KFX delivery](https://github.com/kafkasl/ksync), [Readest reader APIs](https://github.com/readest/readest/tree/main/apps/readest-app/src/app/api/crosspoint), and [CrossPoint Sync API](https://github.com/crosspoint-reader/crosspoint-sync/blob/main/docs/API.md).
