# Memory efficiency

The web service runs progress and discovery jobs in short-lived worker processes.
Book indexes and parser allocations are reclaimed when the worker exits. The
parent holds the existing job lock until completion, receives progress messages,
and checks the worker's exit status. A 15-minute timeout terminates the worker
and its descendants. Normal pending-write recovery still applies to interrupted
progress writes. The worker module is internal; use the Sync page or the existing
preview command rather than invoking it directly.

Catalog entries can defer map validation with `Book.load_mapper`. Manually paused
books skip this callback and reader sessions. Active books still validate the
profile and input fingerprints before opening readers. Automatic status detection
must read positions and therefore still needs the map.

Readers share `Book.coordinates` within a book. Coordinate arrays use 32-bit
unsigned integers and promote to 64-bit if needed, preserving large positions.
The job drops cached coordinates and deferred maps after each book, including
failed syncs. Parser logs that are never displayed are discarded rather than
buffered. Discovery retains at most one accepted candidate's text index; an
ambiguous match cannot produce a map.

## DietPi measurements

Measured with ARM64 CPython 3.13 on a Pi Zero 2 W with 415 MiB usable memory.
The offline workload used three existing maps and an EPUB containing 1,737,922
normalized characters. Staged versions ran sequentially in fresh processes.
Production book settings and remote reading positions were unchanged. These
single-run measurements are indicative, not timing guarantees.

| Stage | Measurement | Before | After |
|---|---|---:|---:|
| Skip paused maps | Three paused books, elapsed | 20.35 s | 0.30 s |
| Skip paused maps | Peak process RSS | 105.3 MiB | 40.5 MiB |
| Worker isolation | Parent RSS after job and garbage collection | 68.5 MiB | 41.0 MiB |
| Compact coordinates | Integer-array payload for large EPUB | 13.26 MiB | 6.63 MiB |
| Shared coordinates | Two readers indexing the same EPUB, elapsed | 9.87 s | 4.97 s |
| Shared coordinates | Peak process RSS | 72.0 MiB | 57.1 MiB |
| Explicit release | Three-map job peak RSS | 72.4 MiB | 71.5 MiB |

The small explicit-release difference is within normal measurement variation;
its regression tests establish deterministic release before the next book, even
when a catalog retains its book objects. Lazy loading already removes most
built-in catalog overlap.

A final web-style offline job sampled aggregate proportional set size (PSS)
across the parent and descendants. Combined peak PSS fell from **98.0 MiB to
91.2 MiB**. Parent PSS after the job fell from **55.7 MiB to 32.2 MiB**. Elapsed
time increased from **18.32 s to 19.88 s**, including the extra process startup.
Workers target idle retention; they do not eliminate memory needed during a job.
PSS and RSS are different measurements and should not be compared directly.

The large EPUB's normalized text and coordinate fingerprint was identical before
and after compaction. Tests also cover 64-bit promotion, shared-index identity,
deferred fingerprint failures, cleanup after reader failures, worker failures,
and timeout termination of nested processes.

## Lightweight web initialization

Plugin descriptors defer service client, authentication, reader, and catalog
imports until the corresponding action is used. Rendering Sync or Setup and
checking connections do not import these implementations. New plugins can use
`placekeeper.plugins.lazy.deferred` to expose callbacks without eager imports.

In fresh DietPi processes, initializing the web app and reading the same real
status snapshot used **40.1 MiB RSS before and 22.5 MiB after**. PSS fell from
**30.5 MiB to 15.3 MiB**, and initialization elapsed time fell from **1.91 s to
0.49 s**. These measure server initialization, not browser JavaScript heap use.
Setup actions can still import their required client libraries when invoked.

The Sync page polls every 30 seconds while idle and every 2.5 seconds during a
job. Hidden tabs stop polling and refresh when visible again. Refresh requests
do not overlap; actions queue a fresh status check when necessary. Unchanged
book controls, sync results, and discovery lists retain their existing DOM nodes.
Idle polling therefore drops from 24 to 2 requests per minute per visible tab.

The lightweight-import regression runs in a fresh Python process. The optional
browser-logic tests use Node's built-in test runner with no additional packages:

```sh
node --test tests/test_web_polling.js
```

## Metadata and status-request efficiency

Book-status metadata is now published once per completed catalog job, and only
if it changed. A synthetic 100-book metadata-only job previously replaced the
status file 100 times on both its first and unchanged subsequent run. It now
replaces it once initially and zero times when unchanged. Progress journals still
save immediately before and after remote writes; their safety checks are unchanged.

Each status request now takes one connection snapshot and reuses it for service
labels, readiness, and discovery availability. For four plugins, this reduces
connection checks from 12 to 4 per request without caching connections between
requests. Readest pagination retains fixed-size page hashes instead of full JSON
page strings when checking for repeated pages.
