import httpx

from ash.cli import main


def test_a2a_check_reports_protocol_readiness(capsys) -> None:
    assert main(["a2a", "check"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Ash A2A protocol v1.0 is ready.\n"
    assert captured.err == ""


def test_a2a_serve_requires_an_operator_token(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ASH_A2A_TOKEN", raising=False)

    assert main(["a2a", "serve"]) == 2
    assert "Set ASH_A2A_TOKEN" in capsys.readouterr().err


def test_a2a_client_network_failure_has_stable_cli_error(monkeypatch, capsys) -> None:
    async def fail(args) -> int:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("ash.commands.a2a.inspect_a2a", fail)

    assert main(["a2a", "inspect", "https://agent.example.com"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "A2A operation failed: connection refused" in captured.err
