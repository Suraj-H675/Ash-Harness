# Releasing Ash

Ash production installs are release-only. A published release is trusted only
when GitHub reports it as immutable, and the public bootstrap verifies the
release asset's SHA-256 digest before executing it.

## Repository prerequisite

Enable **immutable releases** in the repository before publishing the first
release. This is a hard requirement, not an optional hardening step. Check the
current setting with an administrator-authenticated GitHub CLI session:

```bash
gh api \
  -H 'Accept: application/vnd.github+json' \
  -H 'X-GitHub-Api-Version: 2026-03-10' \
  repos/Suraj-H675/Ash-Harness/immutable-releases
```

The response must contain `"enabled": true`. The release workflow also checks
the published release. If GitHub reports it as mutable, the workflow deletes
that release, leaves the existing tag in place, and fails so the same workflow
run can be retried after the repository setting is corrected.

Run this check **before pushing the release tag**. The standard Actions
`GITHUB_TOKEN` is intentionally not granted repository-administration access,
so release CI does not rely on a privileged settings credential just to perform
this preflight.

## Release contract

1. Update `project.version` in `pyproject.toml` and its lockfile as needed.
2. Complete the normal local verification gates before tagging.
3. Create and push a tag named exactly `ash-v<project.version>`.
4. Let `.github/workflows/release.yml` build and publish the release. Do not
   hand-replace release assets or move a release tag.

The release workflow first calls the complete reusable supported-host CI
workflow and cannot enter the publishing job until every required Linux/macOS,
Python-version, packaging, browser, sandbox, LSP, and MCP lane succeeds. The
publishing job then checks that the tag matches the package version, validates
the lockfile and dependencies again, runs its Ubuntu/Python 3.12 lint/type/test
gate, builds the sdist and then the wheel from that fresh sdist, installs the
wheel and requires its exact `ash --version` output to match the release tag,
smoke-tests the wheel, tests the standalone installer on Python 3.10, emits
`SHA256SUMS`, creates artifact provenance, and publishes the assets through a
draft release before verifying that the final release is immutable.

The release also publishes the repository-root `install.sh` as the normal-user
one-line bootstrap. It can bootstrap `uv` and a supported Python runtime when
the host does not already have a usable installer toolchain. It then resolves
the latest immutable release, selects exactly the uploaded `install-ash.py`
asset from the GitHub Releases API, and checks its declared size and SHA-256
digest before execution. The standalone `install-ash.py` asset is copied from
`src/ash/installer.py`.
The standalone installer then resolves exactly one uploaded universal Ash wheel
from that same immutable release, validates its GitHub URL, size, and SHA-256
digest, streams it into a private temporary directory, and independently
verifies the downloaded bytes before pipx/uv run. The managers receive a local
wheel requirement with the same hash fragment as defense in depth; Ash does not
depend on manager-specific URL-hash behavior for integrity. Production
installation therefore consumes the same wheel that the release workflow built
from the fresh sdist, smoke-tested, checksummed, and attested; it does not
perform a second client-side build from the Git tag. The temporary wheel is
removed after the manager finishes, and the installed `ash --version` must also
match the requested `ash-v<version>` release tag.

Re-running the same verified immutable ref repairs the managed environment.
Installing a newer immutable ref upgrades it. Installing an older verified
immutable ref rolls back the managed **package**. Supported capability extras
already present in the managed installation are preserved across upgrade,
repair, and package rollback.

Package rollback does not reverse user-data schema migrations. A release that
advances the durable session schema must document its automatic pre-migration
backup and the compatible restore procedure. For 0.2.0 specifically, the
published 0.1.0 release creates schema v16 databases; opening one with 0.2.0
migrates it through v17 to v18 after creating a validated
`before-v18-migration` backup. Returning to 0.1.0 after that migration requires
restoring the v16 backup as well as installing the older package. See
[Maintenance and recovery](MAINTENANCE.md#roll-back-across-a-session-schema-migration).

## Verify a published release

GitHub CLI can verify both the release attestation and an individual downloaded
asset:

```bash
tag="ash-v0.1.0"  # replace with the release being checked
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

gh release verify "$tag" -R Suraj-H675/Ash-Harness
gh release download "$tag" \
  -R Suraj-H675/Ash-Harness \
  --pattern install-ash.py \
  --dir "$tmp"
gh release verify-asset "$tag" "$tmp/install-ash.py" \
  -R Suraj-H675/Ash-Harness
```

Do not publish a release if any verification step is red. The release workflow
is intentionally fail-closed because Ash's installer and updater are part of
the software-supply-chain trust boundary.
