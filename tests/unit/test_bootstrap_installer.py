from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "install.sh"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_bootstrap_installer_is_posix_shell_and_release_backed() -> None:
    syntax = subprocess.run(
        ["sh", "-n", str(BOOTSTRAP)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert syntax.returncode == 0, syntax.stderr
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'REPOSITORY = "Suraj-H675/Ash-Harness"' in source
    assert 'RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"' in source
    assert 'asset.get("name") == "install-ash.py"' in source
    assert 'payload.get("immutable") is not True' in source
    assert "installer SHA-256 did not match release metadata" in source
    assert "eval " not in source


def test_bootstrap_installer_bootstraps_uv_when_runtime_manager_is_missing(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    home = tmp_path / "home"
    bin_dir.mkdir()
    home.mkdir()
    log = tmp_path / "python-args"

    _write_executable(bin_dir / "uname", "#!/bin/sh\necho Linux\n")
    _write_executable(bin_dir / "dirname", "#!/bin/sh\n/usr/bin/dirname \"$@\"\n")
    for name in ("sh", "mkdir", "chmod"):
        resolved = shutil.which(name)
        assert resolved is not None
        (bin_dir / name).symlink_to(Path(resolved))
    _write_executable(
        bin_dir / "python3",
        f"#!/bin/sh\n"
        "if [ \"${1:-}\" = \"-c\" ]; then exit 0; fi\n"
        f"printf '%s\\n' \"$@\" > {log}\n",
    )
    _write_executable(
        bin_dir / "curl",
        "#!/bin/sh\n"
        "printf '%s\\n' \\\n+        'mkdir -p \"$HOME/.local/bin\"' \\\n+        'printf \"%s\\\\n\" \"#!/bin/sh\" \"exit 0\" > \"$HOME/.local/bin/uv\"' \\\n+        'chmod +x \"$HOME/.local/bin/uv\"'\n",
    )

    completed = subprocess.run(
        ["/bin/sh", str(BOOTSTRAP), "--extra", "browser"],
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": str(bin_dir)},
    )

    assert completed.returncode == 0, completed.stderr
    assert "Preparing Ash's runtime..." in completed.stdout
    assert (home / ".local" / "bin" / "uv").is_file()
    assert log.read_text(encoding="utf-8").splitlines() == [
        "-I",
        "-",
        "--extra",
        "browser",
    ]


def test_release_workflow_publishes_bootstrap_as_attested_asset() -> None:
    source = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "cp install.sh dist/install.sh" in source
    assert source.count("dist/install.sh") >= 3
    assert "sha256sum dist/* > dist/SHA256SUMS" in source


def test_bootstrap_file_is_executable() -> None:
    assert os.access(BOOTSTRAP, os.X_OK)
