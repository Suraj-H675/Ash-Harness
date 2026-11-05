# Crew scheduled worker (Linux and macOS)

Crew responsibilities already have durable `--at`, `--every`, and `--cron` schedules.
Use `ash crew tick --owner OWNER --coworker-id ID` for an explicit one-shot
check, or run a persistent foreground worker for **one coworker**:

```bash
ash crew worker --owner local --coworker-id COWORKER_UUID --poll-seconds 60
```

The worker performs one existing, owner-scoped scheduled tick per polling
interval. If nothing is due, it stays quiet and makes **no model request**.
A due responsibility uses its explicitly granted evidence files, bound profile,
tool-free model, bounded turn, durable claim, and original misfire/recovery
semantics. The worker does not install itself, scan files, listen for webhooks,
make file corrections, or approve actions. An unchanged evidence file is **not**
yet a deterministic change detector: an actual due run may call the model even
when the file has not changed. Use deliberate schedules and provider quotas.

The worker starts only after the explicit command runs. To have it start at
user login and survive closing the terminal, supervise it with your existing
**user** service manager. Do not start it as root.

## Linux: systemd user service

Find the **installed Crew-capable** Ash executable with `command -v ash`.
Create `~/.config/systemd/user/ash-crew-scout.service` with your own absolute
executable path, owner label, and coworker UUID:

```ini
[Unit]
Description=Ash Crew scheduled worker for Scout
StartLimitIntervalSec=120
StartLimitBurst=3

[Service]
Type=simple
ExecStart=/home/USER/.local/bin/ash crew worker --owner local --coworker-id COWORKER_UUID --poll-seconds 60
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now ash-crew-scout.service
systemctl --user status ash-crew-scout.service
journalctl --user -u ash-crew-scout.service --since today
systemctl --user disable --now ash-crew-scout.service
```

The last command disables and stops the worker. User services ordinarily run
with the user manager; operation after logout depends on the host's user-manager
and lingering configuration, not on Ash.

## macOS: user LaunchAgent

Create `~/Library/LaunchAgents/com.ash.crew.scout.plist`, replacing
`/absolute/path/to/ash`, the owner, and coworker UUID:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.ash.crew.scout</string>
  <key>ProgramArguments</key>
  <array>
    <string>/absolute/path/to/ash</string>
    <string>crew</string><string>worker</string>
    <string>--owner</string><string>local</string>
    <string>--coworker-id</string><string>COWORKER_UUID</string>
    <string>--poll-seconds</string><string>60</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
</dict>
</plist>
```

```bash
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.ash.crew.scout.plist"
launchctl print "gui/$(id -u)/com.ash.crew.scout"
launchctl bootout "gui/$(id -u)/com.ash.crew.scout"
```

A LaunchAgent belongs to the logged-in user's GUI session; it is not a
machine-wide background daemon. Stop it with `bootout` before removing the plist.

## Control and inspect

Use `ash crew pause --owner local --coworker-id COWORKER_UUID` to stop
**future claims** while keeping the worker process and stored schedules.
`ash crew resume` re-enables future work; `ash crew archive` permanently
disables new work and causes the worker to exit at its next poll. An already
claimed provider turn may still finish after pause/stop; it is never silently
replayed after uncertainty. Stop the worker with Ctrl-C, SIGTERM or the
service-manager stop command. Inspect `ash crew show`, `ash crew inbox`, and
`ash crew responsibility runs` for persisted outcomes. The inbox is local:
**no remote notification is delivered**.

A worker handles exactly one coworker; give another coworker its own user
service. Use separate names/labels for service definitions. Profile credentials
stay in each coworker's selected profile, never in the service arguments.

## Opt-in change-only evidence monitoring

A scheduled responsibility with explicit workspace evidence can opt in to
change-only checks, avoiding a model request when its inputs and task context
are identical to a previous **completed** scheduled model run:

~~~bash
ash crew tick --owner local --coworker-id COWORKER_UUID --only-if-changed
ash crew worker --owner local --coworker-id COWORKER_UUID \
  --poll-seconds 60 --only-if-changed
~~~

Normal scheduling remains unchanged when this option is absent. The flag
applies to the running worker or current tick, not the saved responsibility.
It requires a nonempty, explicit evidence grant; it does not scan repositories
or infer whether an external CI service has changed.

Ash verifies the latest non-skipped scheduled model run, its owner/coworker
binding, durable two-message transcript, configured model, granted evidence
manifest, responsibility objective/criteria, and current owner-curated brief.
If all still match, it records skipped: evidence_unchanged in the dispatch
journal and advances the existing cursor atomically, **without making a model
request**. View these quiet outcomes with ash crew inbox --include-skipped or
ash crew responsibility runs.

There is no skip without a valid completed prior run: first runs, uncertain
claims, changed evidence/brief/objectives, invalid history, and expired
misfire-grace windows follow the existing conservative admission path.
Current evidence is rechecked before writing the skip; concurrent
schedule changes refuse the skip. This is input change detection, not
semantic validation or an autonomous CI/webhook monitor. It cannot replace
a genuine external event receiver.

## Revising an existing responsibility

Owners can revise the objective, success criteria, scope, and explicit evidence
grant without changing the responsibility ID or schedule. Read its current
revision using the normal responsibility list or show command, then specify
that revision when editing:

~~~bash
ash crew responsibility revise --owner local \
  --coworker-id COWORKER_UUID \
  --responsibility-id RESPONSIBILITY_UUID --expected-revision 1 \
  --objective "Investigate the latest approved CI logs" \
  --success-criterion "Cite the failing assertion" \
  --evidence logs/ci.log
~~~

Each supplied list replaces its entire previous list; omitted fields retain
their values. Use --clear-evidence or --clear-success-criteria to empty a
list. An actual change increments the revision; unchanged values do not.
The owner must use the currently reported revision, so stale editors
cannot overwrite a newer decision.

Ash validates file grants using its existing scoped evidence reader and
serializes revisions against approved workspace file effects. A revision is
refused while a signed event is accepted but undispatched, an event is
dispatching, or a model turn for the responsibility remains claimed/uncertain.
This prevents a previously accepted trigger from silently acquiring broader
file access. Resolve queued work and inspect its ledger before revising, or
create a new responsibility where uncertain work cannot be resolved. Previously
completed runs retain their original prompt and provenance; a previously
proposed file correction is invalidated for future approval/application
if its source responsibility scope, objective, criteria or evidence grant
changes. Archived coworkers and completed responsibilities cannot be revised.

This command intentionally does not edit schedule timing, auth keys, or
existing run history. The current next-fire cursor is preserved; to change
timing, complete the old responsibility and create a newly scheduled one.

## Model usage accounting

Inspect recorded model usage for a single coworker with:

    ash crew usage --owner local --coworker-id COWORKER_UUID

The report joins the coworker's owner-bound durable model-run claims to
its separate profile session accounting, including manual, scheduled and
event-triggered turns and separate model analysis turns. Scheduled skipped
occurrences are shown separately and never counted as provider attempts.
It reports recorded prompt/completion/cache tokens, estimated-token subsets
within the recorded token totals, recorded priced
USD cost, estimated USD cost, and runs without reliable pricing. Completed
turns with no token records are explicitly marked **unmetered**; claims
without confirmed completion are **uncertain**, not treated as free work.

These totals are local historical observations, not a current provider
invoice, account balance, provider quota or budget. Pricing may be unknown,
and model-readiness preflight probes and standalone native tool sessions
are not included in the claimed model-run totals. Never interpret a shown
$0 value as proof of no provider charges when incomplete or unpriced work
exists. No provider request or external network call is made to display this
report. A missing or cross-coworker session binding is rejected rather
than silently included. Enforcing future spending/model-attempt limits is
a separate capability and is not implied by this command.

## Lifetime model-attempt budget

Each coworker can have a durable, owner-controlled limit on model-turn
claims, shared by manual asks, chat turns, investigations, signed events,
and scheduled model responsibilities. It defaults to unlimited.

    ash crew budget show --owner local --coworker-id COWORKER_UUID
    ash crew budget set --owner local --coworker-id COWORKER_UUID \
      --max-attempts 10 --expected-revision 0
    ash crew budget clear --owner local --coworker-id COWORKER_UUID \
      --expected-revision 1

Use the current revision shown by the show command; changes are compare-and-swap,
so stale owner commands cannot silently overwrite later limits. A cap of
zero blocks all new model claims. The counter is lifetime, not per-day:
it includes every completed or uncertain model claim already recorded for
that coworker, including claims made before the cap was configured.
Scheduled no-model skips consume no slots. Decreasing the cap below the
already-used count stops new work without erasing old claims. Clearing the
cap requires an explicit owner command and does not erase history.

Budget admission and durable model-run claiming occur in the same SQLite
write transaction. Two concurrent claimants cannot both consume the last
slot. Exhaustion is refused before model dispatch, with no automatic
retry or rollback of previously claimed work. While a cap is configured,
an optional extra --check-model probe within a model turn is disabled
because that probe would contact the provider before the recorded claim.
The separate explicit crew check-model readiness diagnostic remains an
owner-invoked provider test and is not counted by the turn budget.

This mechanism is not a hard dollar, token, or provider-call budget.
Provider charges and token totals can be late, estimated, or unavailable;
even a claimed turn with an interrupted/unknown outcome can incur cost.
Explicit readiness tests and provider calls outside Crew are not governed
by this lifetime model-claim cap. Use crew usage for local observed
accounting and provider-side account limits for a true financial ceiling.
