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

## Release contract

1. Update `project.version` in `pyproject.toml` and its lockfile as needed.
2. Complete the normal local verification gates before tagging.
3. Create and push a tag named exactly `ash-v<project.version>`.
4. Let `.github/workflows/release.yml` build and publish the release. Do not
   hand-replace release assets or move a release tag.

The workflow checks that the tag matches the package version, validates the
lockfile and dependencies, runs lint/type/tests, builds the sdist and then the
wheel from that fresh sdist, smoke-tests the wheel, tests the standalone
installer on Python 3.10, emits `SHA256SUMS`, creates artifact provenance, and
publishes the assets through a draft release before verifying that the final
release is immutable.

The standalone `install-ash.py` asset is copied from `src/ash/installer.py`.
The public bootstrap selects exactly that uploaded asset from the GitHub
Releases API and checks its declared size and SHA-256 digest before execution.
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
