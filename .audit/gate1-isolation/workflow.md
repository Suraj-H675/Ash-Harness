# Gate 1 isolation slice workflow

- [x] Read the Principles section of the poteto-mode skill.
- [x] Phase A: Frame
- [x] Phase B: Design the workflow
- [x] Phase C: Run the loop
  - [x] Map the runtime's ambient sources and effects.
  - [x] Compare a scrubbed child process with in-process dependency injection.
  - [x] Implement one narrow, reusable subprocess environment boundary.
  - [x] Verify real child processes against ambient credential and profile sentinels.
  - [x] Run runtime and SDK regressions, lint, and type checks.
- [x] Phase D: Keep the audit trail
- [x] Phase E: Verify and hand back

## Definition of done

An owner-keyed coworker handle yields a fresh child process context with per-run HOME, XDG roots, temporary storage, and working directory. The child inherits none of the parent's profile, provider, extension, proxy, Python path, or executable search environment. Two contexts remain disjoint. Runtime execution stays blocked. Focused checks pass. The progress document records exact proof gaps.

## Scope and rigor

Scope is one source module, its unit tests, this workflow record, a decision log, and the Crew progress document. Runtime stays blocked because environment isolation alone cannot stop filesystem traversal, cloud metadata access, network calls, workspace effects, callbacks, or same-user access between child processes. No real credentials or provider requests are permitted. Rigor is high because ambient credentials and extensions cross the runtime boundary.

## Design sketch

`CoworkerProcessEnvironment` is an immutable value with `root`, `home`, `working_directory`, and `temporary_directory` paths. `as_environ()` returns a fresh allowlisted child environment with HOME, XDG roots, temporary paths, and Python user-site protections. It does not copy parent variables or accept overlays. `CoworkerHandle.worker_process_environment()` yields that value while a private temporary root exists. The caller must keep the context open until its child exits and launch a trusted executable by absolute path.

The context carries no provider, session DB, workspace, extension, callback, or approval objects. It does not restrict OS access, so the child can still reach host files and network services with the current user's permissions. Coworker processes also share that OS user and can read one another's temporary directories. The context cannot authorize runtime execution.

## Decision

A scrubbed child process is the lower-complexity starting point because Ash has process-global provider registries, environment readers, and `Path.home()` readers. In-process dependency injection would require plumbing explicit providers, credential access, extension roots, and filesystem roots through many modules, while cloud SDK credential chains would still need separate controls. A child environment can reset those defaults in one place, but a complete runtime boundary also needs OS filesystem and network containment plus authenticated admission. This batch builds only the environment primitive and keeps the runtime block in force.
