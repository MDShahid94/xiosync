import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta

sys.path.insert(0, ".")
from xiosync.platform.tokens import issue_access_token

SESSION_ID = "01a0c52a-bf13-72b3-9a14-3eb304eee463"
ORG_ID = "00000000-0000-7000-8000-000000000000"
ACTOR_ID = "00000000-0000-7000-8000-000000000002"


async def test_ws():
    import json

    import websockets

    token, claims = issue_access_token(
        secret="xiogrid-mac-mini-1-auth-secret-2026-karma",
        session_id=uuid.UUID(SESSION_ID),
        organization_id=uuid.UUID(ORG_ID),
        actor_id=uuid.UUID(ACTOR_ID),
        now=datetime.now(UTC),
        ttl=timedelta(minutes=14),
    )

    ws_url = f"ws://localhost:8000/api/v1/xioview/sessions/{SESSION_ID}/observe?mode=screenshot&token={token}"
    try:
        # First test normal behavior
        async with websockets.connect(ws_url) as ws:
            print("Connected to WS.")
            await ws.send(json.dumps({"type": "click", "x": 100, "y": 100}))
            msg = await ws.recv()
            print("Received normal:", msg)

            # Since there is no actual patchright page running, we should just not get blocked,
            # maybe nothing happens or we get an error about no page.

    except Exception as e:
        print("WS error 1:", e)

    # NOW fake a run in the pool!
    # Wait, the worker and API are running in different processes via uvicorn/worker in nohup!
    # Calling get_runtime_pool() here only modifies the local process pool, not the uvicorn one!
    # Ah! The API process has its own pool!
    print("Done")


if __name__ == "__main__":
    asyncio.run(test_ws())
