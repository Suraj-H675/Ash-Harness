from ash.install import pipx_install_command


def test_pipx_install_command_clears_existing_uv_environment() -> None:
    assert pipx_install_command() == (
        "UV_VENV_CLEAR=1 pipx install --force "
        "'ash-ai @ git+https://github.com/Suraj-H675/Ash-Harness.git'"
    )


def test_pipx_install_command_keeps_sorted_capability_extras() -> None:
    assert pipx_install_command("browser", "server", "browser") == (
        "UV_VENV_CLEAR=1 pipx install --force "
        "'ash-ai[browser,server] @ git+https://github.com/Suraj-H675/Ash-Harness.git'"
    )
