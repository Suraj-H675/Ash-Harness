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
