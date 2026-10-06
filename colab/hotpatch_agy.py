"""
XIOSYNC Worker Hot-Patch — AI Endpoints (Phase 4)
Run this cell ONCE in the Colab notebook to inject:
  POST /ai/install-agy   → install agy + Chrome profile auth
  POST /ai/restore-agy-creds → restore saved credentials
  POST /ai/generate      → native local agy generation
  POST /ai/status        → agy install/auth status

No restart needed — patches the live FastAPI app directly.
"""
import urllib.request, os, sys, importlib, types, json

# ── Pull latest xiorun_agent.py from XIOSYNC ──────────────────────────────────
XIOSYNC_BASE = os.environ.get("XIORUN_XIOSYNC_BASE") or os.environ.get("XIOSYNC_BASE", "")
if not XIOSYNC_BASE:
    try:
        XIOSYNC_BASE = open("/tmp/xio_xiosync_base").read().strip()
    except Exception:
        pass
assert XIOSYNC_BASE, "XIOSYNC_BASE not set — check environment"

_agent_url = f"{XIOSYNC_BASE}/api/v1/workers/xiorun-agent.py"
print(f"Fetching latest xiorun_agent.py from {_agent_url} …", end=" ")
_src = urllib.request.urlopen(_agent_url, timeout=30).read().decode()
print(f"OK ({len(_src):,} bytes)")

# Verify Phase 4 is present
assert "Phase 4" in _src, "Server is not serving Phase 4 code yet"
assert "ai_install_agy" in _src, "ai_install_agy endpoint not found"
print("✅ Phase 4 code confirmed on server")

# ── Extract and re-register just the AI endpoint functions ────────────────────
# Execute the new source in a fresh namespace that shares the live `app` object
import __main__ as _main_mod

# The live app and its globals are in the __main__ module of the uvicorn worker
_live_app = getattr(_main_mod, "app", None)
assert _live_app is not None, "Live FastAPI `app` not found in __main__"

# Build a minimal exec namespace that re-uses the live app + all globals
_ns = dict(vars(_main_mod))   # copy all existing globals (app, logger, os, etc.)
_ns["app"] = _live_app        # ensure same app object is patched

# Execute the full new source — it will re-define and re-register all endpoints
# Because FastAPI deduplicates by route path, re-adding replaces the old handler.
exec(compile(_src, "<hotpatch>", "exec"), _ns)

# Verify new routes are registered
_routes = [r.path for r in _live_app.routes]
_added = [r for r in _routes if r.startswith("/ai/")]
print(f"✅ AI routes registered: {_added}")

# ── Quick smoke test ──────────────────────────────────────────────────────────
import asyncio, httpx

async def _smoke():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9300", timeout=10) as c:
        r = await c.get("/ai/status")
        print(f"GET /ai/status → {r.status_code}: {r.json()}")

asyncio.get_event_loop().run_until_complete(_smoke())
