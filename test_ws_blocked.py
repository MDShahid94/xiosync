import asyncio
import uuid
from datetime import UTC, datetime, timedelta

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

    # Send a request to attach to a mock/fake page if possible
    # We will just see if we can trigger the "interaction_blocked" logic

    # Wait, the WS is on public router, so no token strictly needed for ws endpoint if we can hit it,
    # but wait, the view UI takes the token via JS? Or it relies on session id.

    ws_url = f"ws://localhost:8000/api/v1/xioview/sessions/{SESSION_ID}/observe?mode=screenshot&token={token}"
    try:
        async with websockets.connect(ws_url) as ws:
            print("Connected to WS.")
            # Send click
            await ws.send(json.dumps({"type": "click", "x": 100, "y": 100}))
            # Wait for response
            while True:
                msg = await ws.recv()
                print("Received:", msg)
                data = json.loads(msg)
                if data.get("type") == "interaction_blocked":
                    print("SUCCESS! Interaction was blocked.")
                    break
    except Exception as e:
        print("WS error:", e)


if __name__ == "__main__":
    asyncio.run(test_ws())
