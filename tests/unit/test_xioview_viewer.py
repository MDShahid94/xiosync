from __future__ import annotations

from unittest.mock import patch

import pytest
from xiosync.subsystems.xioview.viewer import _load_template, render_viewer


@pytest.fixture(autouse=True)
def clear_cache():
    _load_template.cache_clear()
    yield
    _load_template.cache_clear()


def test_render_viewer_substitutes_session_id():
    with patch(
        "xiosync.subsystems.xioview.viewer._load_template", return_value="ID: {{SESSION_ID}}"
    ):
        res = render_viewer("sess_123", "ws://url")
        assert res == "ID: sess_123"


def test_render_viewer_substitutes_ws_url():
    with patch("xiosync.subsystems.xioview.viewer._load_template", return_value="URL: {{WS_URL}}"):
        res = render_viewer("sess_123", "ws://myurl")
        assert res == "URL: ws://myurl"


def test_render_viewer_no_template_vars():
    with patch(
        "xiosync.subsystems.xioview.viewer._load_template",
        return_value="URL: {{WS_URL}} ID: {{SESSION_ID}}",
    ):
        res = render_viewer("sess_123", "ws://url")
        assert "{{SESSION_ID}}" not in res
        assert "{{WS_URL}}" not in res


def test_render_viewer_cached():
    with patch(
        "xiosync.subsystems.xioview.viewer.pathlib.Path.read_text", return_value="text"
    ) as mock_read:
        with patch("xiosync.subsystems.xioview.viewer.pathlib.Path.exists", return_value=True):
            render_viewer("s", "u")
            render_viewer("s", "u")
            assert mock_read.call_count == 1
