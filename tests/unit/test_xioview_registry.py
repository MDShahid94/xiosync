from __future__ import annotations

import asyncio
import json
import pytest

from xiosync.subsystems.xioview.registry import XIOViewRegistry, VALID_MODES
from xiosync.subsystems.xioview.protocol import (
    MODE_SCREENSHOT, MODE_CDP_SCREENCAST, MODE_DOM_STREAM,
    MODE_CDP_DOM_SNAPSHOT, MODE_DOM_OVERLAY
)

@pytest.fixture
def registry():
    return XIOViewRegistry()

@pytest.fixture
def queue1():
    return asyncio.Queue()

@pytest.fixture
def queue2():
    return asyncio.Queue()

def test_valid_modes():
    assert VALID_MODES == {
        MODE_SCREENSHOT, MODE_CDP_SCREENCAST, MODE_DOM_STREAM,
        MODE_CDP_DOM_SNAPSHOT, MODE_DOM_OVERLAY
    }
    assert len(VALID_MODES) == 5

def test_add_client_creates_entry(registry, queue1):
    entry = registry.add_client("sess_1", "org_1", MODE_CDP_SCREENCAST, queue1)
    assert entry.session_id == "sess_1"
    assert entry.org_id == "org_1"
    assert entry.mode == MODE_CDP_SCREENCAST
    assert entry.client_count == 1
    assert queue1 in entry.queues

def test_add_client_multiple_clients_same_session(registry, queue1, queue2):
    registry.add_client("sess_1", "org_1", MODE_CDP_SCREENCAST, queue1)
    entry = registry.add_client("sess_1", "org_1", MODE_CDP_SCREENCAST, queue2)
    assert entry.client_count == 2
    assert queue1 in entry.queues
    assert queue2 in entry.queues

def test_remove_client_last_client_stops_session(registry, queue1):
    entry = registry.add_client("sess_1", "org_1", MODE_CDP_SCREENCAST, queue1)
    registry.remove_client("sess_1", queue1)
    assert not entry.active
    assert "sess_1" not in registry._sessions

def test_remove_client_keeps_session_with_remaining(registry, queue1, queue2):
    entry = registry.add_client("sess_1", "org_1", MODE_CDP_SCREENCAST, queue1)
    registry.add_client("sess_1", "org_1", MODE_CDP_SCREENCAST, queue2)
    registry.remove_client("sess_1", queue1)
    assert entry.active
    assert entry.client_count == 1
    assert queue2 in entry.queues
    assert "sess_1" in registry._sessions

@pytest.mark.asyncio
async def test_push_event_reaches_all_queues(registry):
    queues = [asyncio.Queue() for _ in range(3)]
    for q in queues:
        registry.add_client("sess_1", "org_1", MODE_CDP_SCREENCAST, q)
    event = {"msg": "hello"}
    registry.push_event("sess_1", event)
    for q in queues:
        data = await q.get()
        assert json.loads(data) == event

@pytest.mark.asyncio
async def test_push_event_nonexistent_session(registry):
    # Should not raise
    registry.push_event("unknown", {"msg": "hello"})

@pytest.mark.asyncio
async def test_push_event_full_queue_doesnt_crash(registry):
    q1 = asyncio.Queue(maxsize=1)
    q2 = asyncio.Queue(maxsize=1)
    registry.add_client("sess_1", "org_1", MODE_CDP_SCREENCAST, q1)
    registry.add_client("sess_1", "org_1", MODE_CDP_SCREENCAST, q2)
    
    # Fill q1
    q1.put_nowait("fill")
    
    event = {"msg": "hello"}
    # This should not raise an exception even though q1 is full
    registry.push_event("sess_1", event)
    
    data = await q2.get()
    assert json.loads(data) == event

@pytest.mark.asyncio
async def test_push_event_to_org(registry):
    q_org1_a = asyncio.Queue()
    q_org1_b = asyncio.Queue()
    q_org2 = asyncio.Queue()
    
    registry.add_client("sess_1", "org_1", MODE_CDP_SCREENCAST, q_org1_a)
    registry.add_client("sess_2", "org_1", MODE_CDP_SCREENCAST, q_org1_b)
    registry.add_client("sess_3", "org_2", MODE_CDP_SCREENCAST, q_org2)
    
    event = {"type": "test"}
    registry.push_event_to_org("org_1", event)
    
    assert json.loads(q_org1_a.get_nowait()) == event
    assert json.loads(q_org1_b.get_nowait()) == event
    assert q_org2.empty()

def test_set_global_fps(registry):
    entry1 = registry.add_client("sess_1", "org_1", MODE_CDP_SCREENCAST, asyncio.Queue())
    entry2 = registry.add_client("sess_2", "org_1", MODE_CDP_SCREENCAST, asyncio.Queue())
    
    registry.set_global_fps(10.0)
    assert registry._global_fps == 10.0
    assert entry1.fps == 10.0
    assert entry2.fps == 10.0
    
    # clamp > 15 to 15
    registry.set_global_fps(20.0)
    assert registry._global_fps == 15.0
    assert entry1.fps == 15.0
    
    # clamp < 0.1 to 0.1
    registry.set_global_fps(0.0)
    assert registry._global_fps == 0.1
    assert entry2.fps == 0.1

def test_list_sessions(registry):
    registry.add_client("sess_1", "org_1", MODE_CDP_SCREENCAST, asyncio.Queue())
    registry.add_client("sess_1", "org_1", MODE_CDP_SCREENCAST, asyncio.Queue())
    registry.add_client("sess_2", "org_2", MODE_SCREENSHOT, asyncio.Queue())
    
    sessions = registry.list_sessions()
    # Sort by session_id to ensure order
    sessions.sort(key=lambda s: s["session_id"])
    
    assert len(sessions) == 2
    assert sessions[0]["session_id"] == "sess_1"
    assert sessions[0]["org_id"] == "org_1"
    assert sessions[0]["mode"] == MODE_CDP_SCREENCAST
    assert sessions[0]["clients"] == 2
    
    assert sessions[1]["session_id"] == "sess_2"
    assert sessions[1]["org_id"] == "org_2"
    assert sessions[1]["mode"] == MODE_SCREENSHOT
    assert sessions[1]["clients"] == 1
