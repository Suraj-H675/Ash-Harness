from __future__ import annotations

import io
import json
import urllib.error

import pytest

from ash.commands.update import check_for_update, render_update_status


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()


def opener(payload: dict):
    def open_request(request, timeout: int):
        assert request.full_url.endswith("/releases/latest")
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert timeout == 5
        return Response(json.dumps(payload).encode())

    return open_request


def test_update_check_normalizes_release_tag_and_reports_upgrade() -> None:
    status = check_for_update(
        current_version="0.1.0",
        opener=opener(
            {
                "tag_name": "ash-v0.2.0",
                "html_url": "https://github.com/example/release",
            }
        ),
    )
    assert status.update_available is True
    assert status.latest_version == "0.2.0"
    assert "Ash-Harness.git@ash-v0.2.0" in render_update_status(status)
    assert (
        json.loads(render_update_status(status, json_output=True))["update_available"]
        is True
    )


def test_update_check_reports_current_version() -> None:
    status = check_for_update(
        current_version="1.0.0",
        opener=opener(
            {"tag_name": "v1.0.0", "html_url": "https://example.test/release"}
        ),
    )
    assert status.update_available is False
    assert render_update_status(status) == "Ash 1.0.0 is up to date."


def test_update_check_handles_missing_and_invalid_releases() -> None:
    def missing(request, timeout: int):
        raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    with pytest.raises(ValueError, match="No published"):
        check_for_update(current_version="0.1.0", opener=missing)
    with pytest.raises(ValueError, match="invalid release response"):
        check_for_update(current_version="0.1.0", opener=opener({}))
    with pytest.raises(ValueError, match="not a valid version"):
        check_for_update(
            current_version="0.1.0",
            opener=opener(
                {"tag_name": "latest", "html_url": "https://example.test/release"}
            ),
        )
