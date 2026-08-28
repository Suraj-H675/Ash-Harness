from ash.install import install_command, pipx_install_command


def test_install_command_hides_package_manager_repair_details() -> None:
    assert install_command() == (
        "curl -fsSL "
        "https://raw.githubusercontent.com/Suraj-H675/Ash-Harness/"
        "main/src/ash/installer.py | python3 -"
    )


def test_install_command_keeps_sorted_capability_extras_and_ref() -> None:
    assert install_command("browser", "server", "browser", ref="stable-v1") == (
        "curl -fsSL "
        "https://raw.githubusercontent.com/Suraj-H675/Ash-Harness/"
        "main/src/ash/installer.py | python3 - "
        "--extra browser --extra server --ref stable-v1"
    )


def test_old_pipx_helper_routes_callers_to_public_installer() -> None:
    assert pipx_install_command("browser") == install_command("browser")
