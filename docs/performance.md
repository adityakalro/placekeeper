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
