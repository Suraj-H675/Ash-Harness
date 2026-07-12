from ash.cli import main


def test_acp_check_reports_protocol_readiness(capsys) -> None:
    assert main(["acp", "--check"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Ash ACP protocol v1 is ready.\n"
    assert captured.err == ""
