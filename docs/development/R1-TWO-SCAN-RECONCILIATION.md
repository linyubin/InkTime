# R1 / PR-01 — Two-pass photo reconciliation (review only)

This is a **read-only candidate-missing report**, not a deletion workflow or
permission to purge photo_scores. It does not modify the database, photo
files, filelist.txt, or ESP32. No real NAS has been tested.

## Isolated staging procedure

1. Prepare a verified SQLite v2 **staging** copy with the documented online
   backup/restore procedure. Mount a separate, non-production photo tree.
   Do not give the staging account write access to production mounts.
2. Determine the **exact** path root used in the database's photo_scores.path
   records. A historical Windows UNC root is not necessarily the same as the
   current Linux mount path. Pass that value explicitly as --db-root.
3. Choose --minimum-images from an independently established healthy scan
   count. It is a lower bound, not a guarantee that a NAS is mounted.
4. Run two independent recursive scans separated by a configurable delay.
   Any unreadable directory, metadata failure, count below the minimum, or
   disagreement between the scans blocks the report.

Example for a staging database whose stored paths use Linux paths:

    python -m scripts.reconcile_photos \
      --database /staging/inktime/photos.db \
      --images /staging/inktime/photos \
      --db-root /staging/inktime/photos \
      --minimum-images 100 \
      --delay-seconds 60 \
      --report /staging/inktime/reports/reconcile-001.json

For a database with UNC paths, supply the literal stored root, for example
--db-root '\\server\Photos', while --images remains the local staging mount.

The report parent directory must already exist. The report is created with
mode 0600 and **never overwritten**. It must not be inside the photo tree or
replace the database. The JSON contains sensitive photo paths; do not upload
it to a public issue or CI artifact.

## Interpretation

- The two passes must have **identical complete image sets**, not merely equal
  counts. Files with .jpg/.jpeg/.png/.bmp/.webp extensions are considered,
  regardless of analyzer selection/screenshot filters. Symlinks are not followed.
- Only DB records under the exact supplied --db-root are compared. This avoids
  a sibling prefix such as /Photos-private being included in /Photos.
- missing_paths are **candidates for human review**. A stable pair of scans
  does not establish that a NAS is healthy or a photo has been intentionally
  deleted; a consistently stale or partial mount can pass. Check mount identity,
  independent file counts, path mappings, backups and historical source records.
- If the database contains multiple historical path roots, run separate audits
  with explicit mappings and reconcile results manually. The tool intentionally
  does not guess Windows/Linux aliases or issue any DELETE statement.
- No automatic purge, manual purge command, or schema mutation is implemented.
  A later removal workflow, if authorized, needs verified backups, separate
  human confirmation, and independently documented acceptance evidence.

## Automated coverage and acceptance boundary

The Python unit tests use disposable SQLite v2 databases and temporary local
directories. They check two matching scans, divergent scans, scan failure,
minimum-count guard, exact path prefix and UNC mapping, exclusive private
reports, and symlink exclusion. This does not test real NAS disconnects,
mount identity, permissions, SMB remount behavior, concurrent photo writers,
HTTPS/browser behavior, or ESP32 hardware.

**Gate R1 stays OPEN** until an operator performs the NAS recovery/disconnect
drills and the remaining security/device checks and records human approval.

## Rollback

Revert the implementation commit(s) on refactor/architecture-v1 only and
rerun CI. The tool creates no database migration or photo mutation, so code
rollback must not restore/overwrite any database. Preserve audit reports as
private operator evidence; delete them only through an authorized retention
procedure.
