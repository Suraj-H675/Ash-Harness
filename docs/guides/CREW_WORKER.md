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
