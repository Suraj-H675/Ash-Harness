import os

from ash.install import install_command, pipx_install_command


def _installer_prefix() -> str:
    url = (
        "https://raw.githubusercontent.com/Suraj-H675/Ash-Harness/"
        "main/src/ash/installer.py"
    )
    if os.name == "nt":
        return f"irm {url} | py -"
    return f"curl -fsSL {url} | python3 -"


def test_install_command_hides_package_manager_repair_details() -> None:
    assert install_command() == _installer_prefix()


def test_install_command_keeps_sorted_capability_extras_and_ref() -> None:
    assert install_command("browser", "server", "browser", ref="stable-v1") == (
        _installer_prefix() + " --extra browser --extra server --ref stable-v1"
    )


def test_old_pipx_helper_routes_callers_to_public_installer() -> None:
    assert pipx_install_command("browser") == install_command("browser")
