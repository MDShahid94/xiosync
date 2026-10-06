from __future__ import annotations

from xiosync.subsystems.xioview.protocol import (
    MODE_SCREENSHOT, MODE_CDP_SCREENCAST, MODE_DOM_STREAM, MODE_CDP_DOM_SNAPSHOT, MODE_DOM_OVERLAY,
    MSG_FRAME, MSG_CONNECTED, MSG_SESSION_INFO, MSG_INTERACTION_ACK, MSG_INTERACTION_BLOCKED,
    MSG_DOM_EVENT, MSG_DOM_SNAPSHOT, MSG_DOM_OVERLAY, MSG_ACTION_LOG, MSG_KEEPALIVE, MSG_ERROR,
    CTL_MOUSE_MOVE, CTL_MOUSEDOWN, CTL_MOUSEUP, CTL_CLICK, CTL_DBLCLICK, CTL_KEY, CTL_TYPE,
    CTL_SCROLL, CTL_PAUSE_WORKFLOW, CTL_RESUME_WORKFLOW, CTL_SET_FPS,
    BLOCKABLE_CONTROL_TYPES, PAGE_CONTROL_TYPES, DOM_CURSOR_JS
)

def test_mode_constants_defined():
    modes = [MODE_SCREENSHOT, MODE_CDP_SCREENCAST, MODE_DOM_STREAM, MODE_CDP_DOM_SNAPSHOT, MODE_DOM_OVERLAY]
    assert len(set(modes)) == 5
    assert all(isinstance(m, str) for m in modes)

def test_msg_constants_defined():
    msgs = [MSG_FRAME, MSG_CONNECTED, MSG_SESSION_INFO, MSG_INTERACTION_ACK, MSG_INTERACTION_BLOCKED,
            MSG_DOM_EVENT, MSG_DOM_SNAPSHOT, MSG_DOM_OVERLAY, MSG_ACTION_LOG, MSG_KEEPALIVE, MSG_ERROR]
    assert len(set(msgs)) == 11
    assert all(isinstance(m, str) for m in msgs)

def test_ctl_constants_defined():
    ctls = [CTL_MOUSE_MOVE, CTL_MOUSEDOWN, CTL_MOUSEUP, CTL_CLICK, CTL_DBLCLICK, CTL_KEY, CTL_TYPE,
            CTL_SCROLL, CTL_PAUSE_WORKFLOW, CTL_RESUME_WORKFLOW, CTL_SET_FPS]
    assert len(set(ctls)) == 11
    assert all(isinstance(c, str) for c in ctls)

def test_blockable_is_subset_of_page_controls():
    assert BLOCKABLE_CONTROL_TYPES.issubset(PAGE_CONTROL_TYPES)

def test_dom_cursor_js_is_valid_js():
    assert DOM_CURSOR_JS.startswith("(function(){")
    assert "__xio_cur" in DOM_CURSOR_JS
