from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from xiosync.subsystems.xioview.control import (
    _last_move_ts,
    dispatch_control,
    scale_coords,
)


@pytest.fixture
def mock_page():
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value={"w": 1000, "h": 1000})
    page.keyboard = AsyncMock()
    return page


@pytest.fixture
def mock_cdp():
    cdp = AsyncMock()
    return cdp


@pytest.mark.asyncio
async def test_scale_coords_no_cache_evaluates_page(mock_page):
    with patch("xiosync.subsystems.xioview.session_manager.get_attached_info") as mock_info:
        mock_info.return_value = {"stream_width": 1920, "stream_height": 1080}

        x, y = await scale_coords("sess_1", mock_page, 192, 108)

        # 192 * 1000 / 1920 = 100
        # 108 * 1000 / 1080 = 100
        assert x == 100
        assert y == 100
        mock_page.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_scale_coords_with_cache_skips_evaluate(mock_page):
    with patch("xiosync.subsystems.xioview.session_manager.get_attached_info") as mock_info:
        mock_info.return_value = {
            "stream_width": 1920,
            "stream_height": 1080,
            "_viewport_w": 960,
            "_viewport_h": 540,
        }

        x, y = await scale_coords("sess_1", mock_page, 1920, 1080)

        assert x == 960
        assert y == 540
        mock_page.evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_click_sends_press_and_release(mock_page, mock_cdp):
    with (
        patch(
            "xiosync.subsystems.xioview.session_manager.get_playwright_page", return_value=mock_page
        ),
        patch(
            "xiosync.subsystems.xioview.session_manager.get_or_create_cdp_session",
            return_value=mock_cdp,
        ),
        patch("xiosync.subsystems.xioview.control.scale_coords", return_value=(10, 20)),
        patch("xiosync.subsystems.xioview.control.is_run_active_async", return_value=False),
    ):
        res = await dispatch_control(
            "click", {"x": 100, "y": 200, "button": "left"}, "sess_1", "org_1"
        )

        assert res["type"] == "interaction_ack"
        assert res["action"] == "click"

        assert mock_cdp.send.call_count == 2
        call1 = mock_cdp.send.call_args_list[0]
        call2 = mock_cdp.send.call_args_list[1]

        assert call1[0][0] == "Input.dispatchMouseEvent"
        assert call1[0][1]["type"] == "mousePressed"

        assert call2[0][0] == "Input.dispatchMouseEvent"
        assert call2[0][1]["type"] == "mouseReleased"


@pytest.mark.asyncio
async def test_dispatch_scroll_uses_message_coords(mock_page, mock_cdp):
    with (
        patch(
            "xiosync.subsystems.xioview.session_manager.get_playwright_page", return_value=mock_page
        ),
        patch(
            "xiosync.subsystems.xioview.session_manager.get_or_create_cdp_session",
            return_value=mock_cdp,
        ),
        patch("xiosync.subsystems.xioview.control.is_run_active_async", return_value=False),
    ):
        await dispatch_control(
            "scroll", {"x": 123, "y": 456, "deltaX": 0, "deltaY": 100}, "sess_1", "org_1"
        )

        mock_cdp.send.assert_awaited_once()
        args = mock_cdp.send.call_args[0]
        assert args[0] == "Input.dispatchMouseEvent"
        assert args[1]["type"] == "mouseWheel"
        assert args[1]["x"] == 123
        assert args[1]["y"] == 456


@pytest.mark.asyncio
async def test_dispatch_mouse_move_rate_limited(mock_page, mock_cdp):
    _last_move_ts.clear()

    with (
        patch(
            "xiosync.subsystems.xioview.session_manager.get_playwright_page", return_value=mock_page
        ),
        patch(
            "xiosync.subsystems.xioview.session_manager.get_or_create_cdp_session",
            return_value=mock_cdp,
        ),
        patch("xiosync.subsystems.xioview.control.scale_coords", return_value=(10, 20)),
        patch("xiosync.subsystems.xioview.control.is_run_active_async", return_value=False),
    ):
        # First move should go through
        res1 = await dispatch_control("mouse_move", {"x": 1, "y": 1}, "sess_1", "org_1")
        assert mock_cdp.send.call_count == 2  # mouseMoved + evaluate

        # Immediate second move should be dropped
        res2 = await dispatch_control("mouse_move", {"x": 2, "y": 2}, "sess_1", "org_1")
        assert res2 is None
        assert mock_cdp.send.call_count == 2


@pytest.mark.asyncio
async def test_dispatch_blocked_during_active_run(mock_page):
    with (
        patch(
            "xiosync.subsystems.xioview.session_manager.get_playwright_page", return_value=mock_page
        ),
        patch("xiosync.subsystems.xioview.control.is_run_active_async", return_value=True),
    ):
        res = await dispatch_control("click", {}, "sess_1", "org_1")
        assert res["type"] == "interaction_blocked"
        assert res["reason"] == "run_active"


@pytest.mark.asyncio
async def test_dispatch_key_with_modifiers(mock_page):
    with (
        patch(
            "xiosync.subsystems.xioview.session_manager.get_playwright_page", return_value=mock_page
        ),
        patch("xiosync.subsystems.xioview.control.is_run_active_async", return_value=False),
    ):
        res = await dispatch_control(
            "key", {"key": "A", "modifiers": ["Shift", "Control"]}, "sess_1", "org_1"
        )

        assert res["success"] is True
        mock_page.keyboard.down.assert_any_call("Shift")
        mock_page.keyboard.down.assert_any_call("Control")
        mock_page.keyboard.press.assert_called_with("A")
        mock_page.keyboard.up.assert_any_call("Control")
        mock_page.keyboard.up.assert_any_call("Shift")


@pytest.mark.asyncio
async def test_dispatch_type_uses_keyboard_type(mock_page):
    with (
        patch(
            "xiosync.subsystems.xioview.session_manager.get_playwright_page", return_value=mock_page
        ),
        patch("xiosync.subsystems.xioview.control.is_run_active_async", return_value=False),
    ):
        res = await dispatch_control("type", {"text": "hello"}, "sess_1", "org_1")

        assert res["success"] is True
        mock_page.keyboard.type.assert_called_with("hello", delay=20)


@pytest.mark.asyncio
async def test_dispatch_set_fps_updates_registry():
    mock_entry = MagicMock()
    mock_entry.fps = 5.0

    mock_registry = MagicMock()
    mock_registry._sessions = {"sess_1": mock_entry}

    with patch("xiosync.subsystems.xioview.registry.get_registry", return_value=mock_registry):
        await dispatch_control("set_fps", {"fps": 10.0}, "sess_1", "org_1")

        assert mock_entry.fps == 10.0


@pytest.mark.asyncio
async def test_dispatch_retry_on_cdp_failure(mock_page, mock_cdp):
    with (
        patch(
            "xiosync.subsystems.xioview.session_manager.get_playwright_page", return_value=mock_page
        ),
        patch(
            "xiosync.subsystems.xioview.session_manager.get_or_create_cdp_session",
            return_value=mock_cdp,
        ),
        patch("xiosync.subsystems.xioview.control.is_run_active_async", return_value=False),
        patch("xiosync.subsystems.xioview.session_manager.invalidate_cdp_session") as mock_inval,
    ):
        mock_cdp.send.side_effect = [Exception("fail1"), None]

        res = await dispatch_control("scroll", {"deltaY": 10}, "sess_1", "org_1")

        assert mock_inval.call_count == 1
        assert res is None


@pytest.mark.asyncio
async def test_dispatch_pause_workflow_calls_set_run_state():
    with patch("xiosync.subsystems.xioview.control.set_run_state") as mock_set:
        await dispatch_control("pause_workflow", {"run_id": "r1"}, "sess_1", "org_1")
        mock_set.assert_awaited_once_with("r1", "org_1", "PAUSED", ("RUNNING", "PENDING"))
