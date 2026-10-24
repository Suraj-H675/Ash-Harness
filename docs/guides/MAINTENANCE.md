# Maintenance and recovery

Ash keeps software installation separate from user-owned state. Updating,
repairing, or uninstalling the package does not silently erase configuration,
profiles, sessions, credentials, extensions, or other data under ~/.ash.

## Diagnose first

Start with:

    ash doctor
    ash doctor --connect
    ash setup status
    ash config explain

The plain doctor is local-only. The connect form additionally checks the
configured provider route/catalog without making a model completion. Use
ash providers test when you explicitly want one bounded real completion.
Routes without a supported model catalog produce an unverified warning in
doctor --connect, not a connectivity failure. Use ash providers test to check
those routes; a real completion may incur provider charges.

## Update

Check for the latest immutable release:

    ash update

Apply it through Ash's verified installer:

    ash update --apply

Ash verifies release metadata and delegates installation to the existing
managed pipx/uv path. Selected capability extras are preserved.

## Repair the current release

If the installed package is damaged but you want to stay on exactly the current
published version:

    ash repair

Repair reinstalls the exact ash-v<current-version> immutable release through
the same verified installer used for production installation. It does not
upgrade to a newer version and preserves user-owned Ash state.

Editable/source checkouts and versions explicitly marked as development or
local builds are intentionally rejected by ash repair; reinstall those from
their development environment instead.

## Back up and restore sessions

Check the session database:

    ash storage check

Create a consistent backup:

    ash storage backup

Restore only after inspecting the backup and explicitly confirming:

    ash storage restore /path/to/backup --yes

Backups cover the session database. They are not a replacement for backing up
all of ~/.ash when you need a complete user-state archive.

### Turn-journal prompt privacy and older data

New turns keep the original prompt in memory for model execution, but the
durable `turn_journal.user_input` field stores only `<prompt omitted>`. This
field is recovery metadata; turn identity, status, pending calls, checkpoints,
and usage remain available to recovery. Ash does not rewrite older journal
rows when it starts.

To replace old journal input values for one session, stop other Ash processes
using that profile and run:

    ash storage redact-turn-inputs --session SESSION_ID --yes

For an older database that this release has not opened yet, run the cleanup
before ordinary startup if you want its existing pre-migration backup to see
the omission marker as the logical field value. That safety backup remains a
byte copy and can still contain SQLite remnants.

The command changes at most 5,000 `turn_journal.user_input` values for that
session in one transaction. If it reports that more values remain, run it
again until none remain. It preserves turn identities, statuses, timestamps,
usage and recovery metadata, tool calls, checkpoints, and transcript messages.
After each batch it attempts to checkpoint and truncate the live SQLite WAL.
If that checkpoint cannot finish, the logical update is still committed and
the command says to rerun it with other Ash processes stopped. The command does
not create a backup, because that would copy the values being removed. It does
not change the schema or invoke the automatic pre-migration backup path.

This is a logical database update, not secure erasure. A successful WAL
checkpoint can settle the live WAL, but SQLite free pages, old WAL contents,
snapshots, and storage media may retain earlier bytes. Existing backups,
pre-restore `.raw` files, filesystem snapshots, and other copies are left
untouched and may contain the original prompt. A restore from an older backup
can put the old logical values back; run the command again if that is the
chosen retention policy. Ash does not claim any old or new backup is sanitized,
and a later schema migration's byte-copy safety backup may still include
SQLite remnants. The next ordinary schema upgrade keeps the existing
backup-before-migration behavior: if old rows have not been redacted, that
backup can contain their original journal text; after cleanup, it may still
contain SQLite remnants.

The guarantee is limited to `turn_journal.user_input`. Transcript messages
continue to use Ash's pattern-based redaction, which is not a general detector
for arbitrary sensitive text. Imported message content and planner-generated
sprint records are separate persistence paths; this command does not sanitize
them. In particular, a sprint planner can produce durable contract text that
echoes a prompt, so do not treat journal cleanup as a complete removal of all
prompt-derived session data.

### Recovery after suspected SQLite corruption on 0.4.0

Ash 0.4.0 could release SQLite advisory locks when multiple Ash processes used
the same database, potentially invalidating active WAL state. Upgrade to 0.4.1
before resuming concurrent processes. The upgrade prevents the reproduced
failure path; it cannot repair previously damaged data.

If database corruption is suspected, stop all Ash processes using that profile
and preserve a copy of the affected files before attempting recovery. Diagnose
the affected database and restore a verified, consistent backup if needed;
do not assume a package reinstall will repair it. `ash storage check` and
`ash storage restore` apply to the session database, not every other Ash-owned
SQLite database. Back up the complete profile if other durable state is affected.

### Roll back across a session-schema migration

Installing an older immutable Ash release rolls back the **package**, not a
database schema that a newer release has already migrated. Before every session
schema migration, Ash creates a validated backup beside the session database
with a name such as:

    sessions.db.before-v18-migration.<timestamp>.backup

For the 0.2.0 upgrade, a database created by the published 0.1.0 release starts
at schema v16; 0.2.0 migrates it through v17 to v18. The automatic
`before-v18-migration` backup therefore preserves the compatible v16 state
needed by 0.1.0. If you must return to 0.1.0 after 0.2.0 has opened the database:

1. Stop all Ash processes using that profile/database.
2. Identify and inspect the `before-v18-migration` backup created by the 0.2.0
   migration.
3. Restore that backup with `ash storage restore /path/to/backup --yes` while
   still using a version that can read both schemas.
4. Immediately install the older immutable release. Do not start the newer Ash
   again between restoring the older database and rolling the package back, or
   it will migrate the database forward again.

If no compatible pre-migration backup exists, do not force an older binary to
open the newer schema. Keep the newer release installed or recover from another
known-good backup instead.

For **0.3.1 → 0.4.0**, the published 0.3.1 release stores sessions with schema
v18, while 0.4.0 migrates them to v19 (persisted per-session permission mode).
The automatic `sessions.db.before-v19-migration.<timestamp>.backup` preserves
the v18 state. To roll back to 0.3.1, follow the same four steps above using
this **v18 backup** while 0.4.0 is still installed, then install the immutable
0.3.1 package. Changes made only after the v19 migration are not present in
that earlier backup; export or preserve newer work separately before restoring.

For **0.4.2 → 0.4.3**, the session schema advances from v19 to v20 to retain
archived remote MCP task handles. Ash creates a
`sessions.db.before-v20-migration.<timestamp>.backup` before migrating an
existing database. If you must roll back to 0.4.2, preserve newer work
separately, restore this v19 backup while still running 0.4.3, then install
the immutable 0.4.2 package. Do not start 0.4.3 against that database again
before reinstalling 0.4.2, or it will migrate the schema forward.

For **0.4.6 → 0.5.0**, the session schema advances
from v20 to v21. Before migration, Ash creates a
`sessions.db.before-v21-migration.<timestamp>.backup`. Migration adds the
nullable `after_mode` column to `file_checkpoints`. It preserves the saved
content and `before_mode` for existing checkpoints. Their `after_mode` value is
null, so recovery keeps the previous hash-based check.

To return to 0.4.6 after the migration:

1. Stop all Ash processes that use the profile or database.
2. Preserve work created after the migration separately. The v20 backup does
   not include it.
3. Restore the `before-v21-migration` backup with
   `ash storage restore /path/to/backup --yes` while the newer release is
   installed.
4. Immediately install the immutable 0.4.6 package. Do not start the newer
   release again before installing 0.4.6, or it will migrate the database to
   v21 again.

If no compatible v20 backup exists, keep the newer release installed or
restore another known-good v20 backup. Do not start 0.4.6 against a v21
database.

## Reset selected default-profile state

Ash reset is deliberately selective:

    ash reset --config
    ash reset --sessions
    ash reset --cache
    ash reset --all

The --all flag selects those three reset categories together. It does not mean
"delete every Ash-owned file": named profiles and installed extensions are
retained. The confirmation prompt states that boundary before deletion.

Use profile-specific commands when you intend to remove a named profile. Do not
use reset as an uninstall mechanism.

## Uninstall the package

Ash does not wrap package-manager removal. Use the manager that owns the
installation:

    pipx uninstall ash-ai

or:

    uv tool uninstall ash-ai

Package-manager uninstall removes the installed Ash package/environment. It
intentionally leaves ~/.ash user data in place so reinstalling does not destroy
profiles, sessions, credentials, extensions, trust decisions, or history.

If you want a complete privacy/data removal as well, first back up anything you
need, then remove the remaining ~/.ash directory yourself only after verifying
that you no longer need any Ash profile or session data.
