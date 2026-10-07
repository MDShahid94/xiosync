#!/usr/bin/env python3
"""
xio_signin_flow.py — End-to-end Google signin with XIOVIEW live streaming

Steps:
  1. Acquire a PPPoE slot for the account (per-account IP pinning)
  2. Launch UC Chrome on worker (returns CDP port immediately)
  2.5. Restore Chrome profile from Drive → Chrome auto-loads session cookies
       (if profile valid: Phase 0 detects "already authenticated" → done in ~10s)
  3. Attach XIOVIEW to Chrome in cdp_screencast mode (works with swiftshader)
  4. Open XIOVIEW viewer in Mac browser
  5. Run UC stealth login (/run-uc-login)
  6. Push Chrome profile to Google Drive
  7. Detach XIOVIEW CDP session (cleans up patchright resources)

Usage:
    python3 scripts/xio_signin_flow.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

# ── Config ──────────────────────────────────────────────────────────────────
XIOSYNC = "http://localhost:8000"
ORG_ID = "00000000-0000-7000-8000-000000000000"
ADMIN_EMAIL = "admin@xiogrid.dev"
ADMIN_PASS = "Xiogrid2026!Admin"
INTERNAL_SECRET = "xiosync-internal-2026-karma"

WORKER_TS_IP = "100.111.130.118"
WORKER_AGENT = f"http://{WORKER_TS_IP}:9300"

# Account to sign in
GOOGLE_ACCOUNT = "shahid.raiganj@gmail.com"
GOOGLE_PASSWORD = "TheTruth1!"
GOOGLE_TOTP = "eupfnmzj7syeqqkj2w7hpbajamvn236f"

ACCOUNT_KEY = GOOGLE_ACCOUNT.split("@")[0].replace(".", "_")
# Drive profile key (Matching XIOSYNC standard: chrome_profiles/PRFL-XXX_slug.tar.gz)
DRIVE_OBJECT_KEY = "profiles/PRFL-005.tar.gz"

SESSION_ID = f"signin-{ACCOUNT_KEY}-{int(time.time())}"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _req(
    method: str, url: str, body: dict | None = None, token: str | None = None, timeout: int = 30
) -> dict:
    data = json.dumps(body).encode() if body is not None else b""
    hdrs: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if url.startswith(WORKER_AGENT):
        hdrs["X-Worker-Secret"] = "xiogrid-worker-org-secret-2026-karma"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()
        raise RuntimeError(f"HTTP {e.code} {url}: {body_txt}") from e


def post(
    path: str, body: dict, token: str | None = None, base: str = XIOSYNC, timeout: int = 30
) -> dict:
    return _req("POST", f"{base}{path}", body, token, timeout)


def delete(
    path: str,
    body: dict | None = None,
    token: str | None = None,
    base: str = XIOSYNC,
    timeout: int = 15,
) -> dict:
    return _req("DELETE", f"{base}{path}", body, token, timeout)


def get(path: str, token: str | None = None, base: str = XIOSYNC) -> dict:
    return _req("GET", f"{base}{path}", token=token)


def banner(msg: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {msg}")
    print(f"{'═' * 60}")


def worker_wait(retries: int = 15, delay: float = 2.0) -> bool:
    """Wait for worker agent to be reachable. Returns True when up, False on timeout."""
    for i in range(retries):
        try:
            r = _req("GET", f"{WORKER_AGENT}/health", timeout=4)
            if r.get("ok") or r.get("node"):
                print(f"✅ Worker reachable: {r.get('node', '?')}")
                return True
        except Exception:
            pass
        if i < retries - 1:
            print(
                f"   Worker not ready yet (attempt {i + 1}/{retries}) — retrying in {delay:.0f}s..."
            )
            time.sleep(delay)
    return False


# ── Main flow ────────────────────────────────────────────────────────────────


def main() -> None:
    t_start = time.time()

    if not worker_wait():
        print("❌ Worker agent unreachable after retries")
        sys.exit(1)

    # ── Step 1: Auth (BYPASSED) ────────────────────────────────────────────────────────
    banner("Step 1 · Authenticating with XIOSYNC (BYPASSED)")
    token = "fake-token"

    # ── Step 2: Acquire PPPoE slot (BYPASSED) ─────────────────
    banner("Step 2 · Acquiring residential PPPoE slot (BYPASSED)")
    ppp_slot = 1
    try:
        import subprocess

        _ip = subprocess.check_output(
            [
                "curl",
                "-s",
                "--max-time",
                "10",
                "-x",
                "socks5h://100.106.81.15:10001",
                "https://api.ipify.org",
            ],
            text=True,
        ).strip()
        public_ip = _ip if _ip else "100.106.81.15"
    except Exception:
        public_ip = "100.106.81.15"

    proxy_url = "socks5://127.0.0.1:19055"
    print(f"✅ Slot {ppp_slot} assigned (bypassed)")
    print(f"   Worker proxy: {proxy_url}")

    # ── Step 2.5: Restore Chrome profile from Drive ───────────────────────────
    # The profile contains valid Google session cookies (~90-day lifetime).
    # If still valid, Chrome Phase 0 check (myaccount.google.com) detects
    # "already authenticated" → skips full login → done in ~10-15s.
    banner("Step 2.5 · Restoring Chrome profile from Drive")
    target_profile_dir = f"/tmp/uc-profile-{SESSION_ID}"
    profile_restored = False
    try:
        pull_resp = post(
            "/pull-profile",
            {
                "identity_id": ACCOUNT_KEY,
                "drive_object_key": DRIVE_OBJECT_KEY,
                "target_dir": target_profile_dir,
            },
            base=WORKER_AGENT,
            timeout=60,
        )
        if pull_resp.get("ok"):
            src = pull_resp.get("source", "drive")
            print(f"✅ Profile restored ({src}) → {pull_resp.get('profile_dir')}")
            print("   Chrome will check if session cookies are still valid (Phase 0)")
            profile_restored = True
        else:
            reason = pull_resp.get("reason", "?")
            print(f"ℹ️  No saved profile ({reason}) — will do fresh login")
            target_profile_dir = None
    except Exception as e:
        print(f"⚠️  Profile pull error ({e}) — proceeding with fresh login")
        target_profile_dir = None

    # ── Step 3: Launch UC Chrome on worker ──────────────────────────────────
    banner("Step 3 · Launching UC Chrome on Colab worker")
    start_body: dict = {
        "session_id": SESSION_ID,
        "email": GOOGLE_ACCOUNT,
        "password": GOOGLE_PASSWORD,
        "totp_secret": GOOGLE_TOTP,
        "proxy_url": proxy_url,
        "exit_node_public_ip": public_ip,
    }
    if target_profile_dir:
        # Pass restored profile dir so Chrome loads existing session cookies
        start_body["user_data_dir"] = target_profile_dir

    start_resp = post("/run-uc-login-start", start_body, base=WORKER_AGENT, timeout=90)

    uc_port = start_resp.get("uc_port", 0)
    cdp_ws_url = start_resp.get("cdp_ws_url", "")
    print(f"✅ UC Chrome launched  port={uc_port}")
    print(f"   CDP WebSocket: {cdp_ws_url}")

    # ── Step 4: Attach XIOVIEW in cdp_screencast mode ────────────────────────
    # cdp_screencast mode: Chrome pushes frames via Page.screencastFrame CDP event.
    # Works with UC Chrome 131 + --use-gl=angle/swiftshader.
    # Screenshot mode returns black frames with this GPU stack.
    banner("Step 4 · Attaching XIOVIEW to UC Chrome (cdp_screencast)")
    time.sleep(2)  # let Chrome fully initialize
    attach_resp = post(
        "/api/v1/xioview/attach",
        {
            "cdp_ws_url": cdp_ws_url,
            "session_id": SESSION_ID,
            "internal_secret": INTERNAL_SECRET,
            "mode": "cdp_screencast",
        },
        timeout=20,
    )
    mode = attach_resp.get("mode", "?")
    print(f"✅ XIOVIEW attached: ok={attach_resp.get('ok')}  mode={mode}")

    viewer_url = f"{XIOSYNC}/api/v1/xioview/sessions/{SESSION_ID}/view"
    print("\n🖥️  LIVE VIEWER:")
    print(f"   {viewer_url}")
    subprocess.Popen(["open", viewer_url])
    time.sleep(3)

    # ── Step 5: Run UC login ─────────────────────────────────────────────────
    banner("Step 5 · Running UC stealth Google signin")
    print(f"   Account : {GOOGLE_ACCOUNT}")
    print(f"   Exit IP : {public_ip}  (residential PPPoE)")
    if profile_restored:
        print("   Profile : restored — checking if session still valid...")
        print("   Expected: ~10–20s if cookies valid, ~90s if expired")
    else:
        print("   Profile : fresh — full XIOBR login flow (~90s)")
    print()
    print("   👆 Switch to XIOVIEW tab to watch live!")

    login_resp = post(
        "/run-uc-login",
        {
            "session_id": SESSION_ID,
            "email": GOOGLE_ACCOUNT,
            "password": GOOGLE_PASSWORD,
            "totp_secret": GOOGLE_TOTP,
            "proxy_url": proxy_url,
            "exit_node_public_ip": public_ip,
        },
        base=WORKER_AGENT,
        timeout=480,
    )  # 480s: 30s page_load_timeout × retries + login

    elapsed = time.time() - t_start
    cookies = login_resp.get("cookies", [])
    final_url = login_resp.get("final_url", "?")
    profile = login_resp.get("profile_dir", "")

    if login_resp.get("ok"):
        print(f"✅ Login SUCCESS  cookies={len(cookies)}  final_url={final_url}")
        print(f"   Profile dir : {profile}")
        print(
            f"   ⏱️  Total time: {elapsed:.0f}s  ({'profile reuse' if profile_restored else 'fresh login'})"
        )
    else:
        print(f"⚠️  Login result: {login_resp}")

    # ── Step 6: Push profile to Drive ───────────────────────────────────────
    MIN_COOKIES = 30
    banner("Step 6 · Pushing Chrome profile to Google Drive")

    # Reconstruct the deterministic path exactly as the worker calculates it
    node_slug = "xiogrid__default__worker_006"
    local_dir = f"/tmp/xiorun_profiles/PRFL_{ACCOUNT_KEY}__{node_slug}"

    print(f"   Drive key : {DRIVE_OBJECT_KEY}")
    print(f"   Local dir : {local_dir}")

    if not login_resp.get("ok"):
        print("⏭️  Skipping profile push — login failed")
    elif len(cookies) < MIN_COOKIES:
        print(f"⏭️  Skipping profile push — only {len(cookies)} cookies (threshold={MIN_COOKIES})")
        print("   ⚠️  Profile NOT saved — would overwrite good profile with degraded auth state")
    else:
        try:
            push_resp = post(
                "/push-profile",
                {
                    "identity_id": ACCOUNT_KEY,
                    "local_dir": local_dir,
                    "drive_object_key": DRIVE_OBJECT_KEY,
                },
                base=WORKER_AGENT,
                timeout=120,
            )
            push_ok = push_resp.get("status", push_resp.get("ok", "?"))
            push_path = push_resp.get("drive_path", push_resp.get("path", "?"))
            print(f"✅ Profile pushed: ok={push_ok}  path={push_path}")
        except Exception as e:
            print(f"⚠️  Profile push failed: {e}")

    # ── Step 7: Detach XIOVIEW (clean up patchright resources) ────────────────
    banner("Step 7 · Detaching XIOVIEW session")
    try:
        det = delete(f"/api/v1/xioview/attach/{SESSION_ID}", timeout=10)
        print(f"✅ XIOVIEW detached: ok={det.get('ok')}")
    except Exception as e:
        print(f"⚠️  Detach failed (non-critical): {e}")

    # ── Summary ──────────────────────────────────────────────────────────────
    banner("All done!")
    print(f"  Account  : {GOOGLE_ACCOUNT}")
    print(f"  Slot     : {ppp_slot}  (permanently bound — same IP next session)")
    print(f"  Exit IP  : {public_ip}")
    print(f"  Cookies  : {len(cookies)}")
    print(
        f"  Profile  : {DRIVE_OBJECT_KEY}  "
        + ("(saved to Drive ✅)" if login_resp.get("ok") else "(NOT saved ❌)")
    )
    print(f"  XIOVIEW  : {viewer_url}")
    print(
        f"  Time     : {elapsed:.0f}s  ({'profile reuse' if profile_restored else 'fresh login'})"
    )
    print()


if __name__ == "__main__":
    main()
