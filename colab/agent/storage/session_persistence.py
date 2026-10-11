"""Session persistence, domain eviction, cookie injection, and cascade checks."""

from __future__ import annotations

import asyncio
import datetime as _dt
import glob as _glob_cc
import json
import logging
import os
import re as _re_cc
import shutil as _sh_ps
import tarfile as _tf_cc
import tempfile as _tmp_ps
import time
from typing import Any
import urllib.request

from fastapi import HTTPException
from pydantic import BaseModel

logger = logging.getLogger("xiorun_agent.storage.session_persistence")

DRIVE_ROOT = os.environ.get("XIO_DRIVE_ROOT", "/content/drive/MyDrive/XIOSYNC-Shared")
XIOSYNC_BASE = os.environ.get("XIOSYNC_BASE", os.environ.get("XIORUN_XIOSYNC_BASE", ""))
XIOSYNC_TOKEN = os.environ.get("WORKER_SECRET", os.environ.get("XIORUN_XIOSYNC_TOKEN", ""))
NODE_NAME = os.environ.get("NODE_NAME", os.environ.get("XIO_NODE_NAME", "colab-agent"))


class EvictDomainRequest(BaseModel):
    profile_dir: str | None = None
    domain_pattern: str
    cascade: bool = True


async def evict_domain_endpoint(req: EvictDomainRequest) -> dict[str, Any]:
    """Evict a domain's cookies from a profile."""
    from xiosync.subsystems.xiorun.domain_eviction import evict_domain_full

    result = evict_domain_full(
        profile_dir=req.profile_dir,
        state=None,
        domain_pattern=req.domain_pattern,
        cascade=req.cascade,
    )
    logger.info(f"Domain eviction: {req.domain_pattern} -> {result}")
    return result


async def inject_session_state(
    req: dict[str, Any],
    sessions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inject cookie state into a running browser session via CDP."""
    session_id = req.get("session_id")
    cookies = req.get("cookies", [])
    registry = sessions or {}
    if not session_id or session_id not in registry:
        raise HTTPException(status_code=404, detail="Session not found")
    sess = registry[session_id]
    cdp_url = sess.get("cdp_ws") or sess.get("cdp_ws_url")
    if not cdp_url:
        raise HTTPException(status_code=400, detail="No CDP endpoint")
    try:
        from patchright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
            await context.add_cookies(cookies)
            count = len(cookies)
            logger.info(f"Injected {count} cookies into session {session_id}")
            return {"injected": count, "session_id": session_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Session Cascade Check ─────────────────────────────────────────────────────
# Pre-login validation: check if existing session is still valid before
# launching a full UC Chrome login. 4 levels (cheapest → most expensive):
#   1. Local profile dir exists on disk
#   2. Live browser verify via patchright → myaccount.google.com DOM check
#   3. Pull profile from Drive FUSE via ChromeProfileStore.restore()
#   4. Not found → proceed to full login


class CascadeCheckRequest(BaseModel):
    identity_id: str
    email: str
    proxy_url: str | None = None


async def session_cascade_check(
    req: CascadeCheckRequest,
    drive_root: str | None = None,
    node_name: str | None = None,
    ssh_proxy_url: str | None = None,
) -> dict[str, Any]:
    """Pre-login session validation. Returns {valid, profile_dir, level}."""
    d_root = drive_root or DRIVE_ROOT
    n_name = node_name or NODE_NAME

    _id_short = req.identity_id.replace("-", "")[:16]
    _node_slug = _re_cc.sub(r"[^a-zA-Z0-9-]", "-", n_name or "default")

    # Level 1: Local profile dir exists
    _local_matches = sorted(_glob_cc.glob(f"/tmp/xiorun_profiles/PRFL_{_id_short}*"))
    _profile_dir = str(_local_matches[-1]) if _local_matches else None

    if _profile_dir and os.path.isdir(_profile_dir):
        logger.info(f"cascade-check: L1 local profile found: {_profile_dir}")

        # Level 2: Live verify — launch patchright with persistent context, navigate to myaccount
        # CRITICAL: use SOCKS5 proxy — cookies were created via the PPPoE exit IP,
        # so verification must go through the same exit IP or Google will reject them.
        try:
            from patchright.async_api import async_playwright

            _l2_proxy = ssh_proxy_url or os.environ.get("XIORUN_PROXY", "socks5://127.0.0.1:19055")
            async with async_playwright() as pw:
                # MUST use launch_persistent_context for a user-data-dir profile.
                # browser.new_context(user_data_dir=...) is NOT valid in patchright.
                context = await pw.chromium.launch_persistent_context(
                    _profile_dir,
                    headless=True,
                    proxy={"server": _l2_proxy} if _l2_proxy else None,
                    args=[
                        "--no-sandbox",
                        "--disable-gpu",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                    viewport={"width": 1440, "height": 900},
                    no_viewport=False,
                )
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(
                    "https://myaccount.google.com/", wait_until="networkidle", timeout=20000
                )
                await asyncio.sleep(1.5)

                prefix = req.email.split("@")[0].lower()
                body_text = await page.evaluate("document.body.innerText.toLowerCase()")
                is_valid = prefix in body_text and "sign in" not in (await page.title()).lower()

                await context.close()

                if is_valid:
                    logger.info(f"cascade-check: L2 live verify PASSED for {req.email}")
                    return {"valid": True, "profile_dir": _profile_dir, "level": "LIVE_VERIFY"}
                else:
                    logger.info(f"cascade-check: L2 live verify FAILED for {req.email}")
        except Exception as e:
            logger.warning(f"cascade-check: L2 verify error: {e}")

    # Level 3: Pull from Drive FUSE — canonical path is profiles/PRFL-NNN.tar.gz
    # Also check legacy chrome_profiles/PRFL_{hex}.tar.gz as a fallback.
    try:
        _profile_dir = f"/tmp/xiorun_profiles/PRFL_{_id_short}__{_node_slug}"
        os.makedirs(_profile_dir, exist_ok=True)

        # Canonical: scan profiles/PRFL-*.tar.gz and match by email via fingerprint JSON.
        _profiles_dir = os.path.join(d_root, "profiles")
        _found_tar = None
        _fallback_tar = None
        if os.path.isdir(_profiles_dir):
            _candidates = sorted(
                [
                    _p
                    for _p in os.listdir(_profiles_dir)
                    if _p.endswith(".tar.gz") and _p.startswith("PRFL-")
                ],
                reverse=True,  # highest serial first (PRFL-003 before PRFL-002)
            )
            for _p in _candidates:
                _tar_path = os.path.join(_profiles_dir, _p)
                _prfl_stem = _p[: -len(".tar.gz")]  # e.g. "PRFL-003"
                _fp_path = os.path.join(_profiles_dir, f"{_prfl_stem}.fingerprint.json")
                if os.path.isfile(_fp_path):
                    try:
                        with open(_fp_path) as _fp_f:
                            _fp_data = json.load(_fp_f)
                        if _fp_data.get("email", "").lower() == req.email.lower():
                            _found_tar = _tar_path
                            logger.info(
                                f"cascade-check: L3 fingerprint match {_prfl_stem} → {req.email}"
                            )
                            break
                    except Exception:
                        pass
                if _fallback_tar is None:
                    _fallback_tar = _tar_path  # highest-serial tar as fallback

            if not _found_tar and _fallback_tar:
                _found_tar = _fallback_tar
                logger.info(
                    f"cascade-check: L3 no fingerprint match — using fallback {os.path.basename(_found_tar)}"
                )

        # Legacy fallback: chrome_profiles/PRFL_{id_short}.tar.gz
        if not _found_tar:
            _legacy = os.path.join(d_root, f"chrome_profiles/PRFL_{_id_short}.tar.gz")
            if os.path.exists(_legacy):
                _found_tar = _legacy

        if _found_tar:
            logger.info(f"cascade-check: L3 Drive profile found: {_found_tar}")
            try:
                with _tf_cc.open(_found_tar, "r:gz") as tf:
                    tf.extractall(path="/tmp/xiorun_profiles")
                logger.info(f"cascade-check: L3 profile extracted to {_profile_dir}")
                return {
                    "valid": False,
                    "profile_dir": _profile_dir,
                    "level": "DRIVE_PULL",
                    "needs_verify": True,
                    "source_tar": _found_tar,
                }
            except Exception as _tex:
                logger.warning(f"cascade-check: L3 extract failed: {_tex}")
        else:
            logger.info("cascade-check: L3 no Drive profile found in profiles/ or chrome_profiles/")
    except Exception as e:
        logger.warning(f"cascade-check: L3 Drive check error: {e}")

    # Level 4: Not found — proceed to full login
    _profile_dir = f"/tmp/xiorun_profiles/PRFL_{_id_short}__{_node_slug}"
    os.makedirs(_profile_dir, exist_ok=True)
    return {"valid": False, "profile_dir": _profile_dir, "level": "NOT_FOUND"}


# ── Persist Session ────────────────────────────────────────────────────────────
# Post-login persistence: save cookies to vault + Chrome profile to Drive.
# Uses existing xiorun subsystem modules (session_state.py, profile_store.py).


class PersistSessionRequest(BaseModel):
    identity_id: str
    org_id: str = "00000000-0000-7000-8000-000000000000"
    email: str | None = None  # used for fingerprint.json
    cookies: list[dict[str, Any]] = []
    profile_dir: str | None = None
    page_url: str | None = None


async def persist_session(
    req: PersistSessionRequest,
    drive_root: str | None = None,
    node_name: str | None = None,
    xiosync_base: str | None = None,
    xiosync_token: str | None = None,
) -> dict[str, Any]:
    """Save cookies to vault + Chrome profile tar to Drive FUSE."""
    d_root = drive_root or DRIVE_ROOT
    n_name = node_name or NODE_NAME
    x_base = xiosync_base or XIOSYNC_BASE
    x_token = xiosync_token or XIOSYNC_TOKEN

    results: dict[str, Any] = {
        "cookie_saved": False,
        "profile_saved": False,
        "cookie_count": 0,
        "profile_key": None,
    }

    # 1. Save cookies to vault via SessionStateIO
    if req.cookies:
        try:
            # Build DB engine — connect to XIOSYNC PostgreSQL
            _db_url = os.environ.get("DATABASE_URL", "")
            if not _db_url and x_base:
                # On workers, use the XIOSYNC API to persist instead of direct DB
                _api_url = f"{x_base}/api/v1/vault/secrets"
                _payload = json.dumps(
                    {
                        "organization_id": req.org_id,
                        "key": f"identities/{req.identity_id}/cookie_state",
                        "value": json.dumps({"cookies": req.cookies, "origins": []}),
                        "secret_type": "cookie_state",
                    }
                ).encode()
                _rq = urllib.request.Request(
                    _api_url,
                    data=_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {x_token}",
                    },
                    method="PUT",
                )
                try:
                    with urllib.request.urlopen(_rq, timeout=10):
                        results["cookie_saved"] = True
                        results["cookie_count"] = len(req.cookies)
                        logger.info(
                            f"persist-session: cookies saved to vault ({len(req.cookies)} cookies)"
                        )
                except Exception as _ve:
                    logger.warning(f"persist-session: vault API save failed: {_ve}")
            elif _db_url:
                # Direct DB access (if running on server or DATABASE_URL set)
                from sqlalchemy import create_engine as _ce_ps

                _engine = _ce_ps(_db_url)
                from xiosync.subsystems.xiorun.session_state import SessionStateIO

                sio = SessionStateIO(_engine)
                sio.save(
                    identity_id=req.identity_id,
                    org_id=req.org_id,
                    state={"cookies": req.cookies, "origins": []},
                    page_url=req.page_url,
                )
                results["cookie_saved"] = True
                results["cookie_count"] = len(req.cookies)
                logger.info(f"persist-session: cookies saved to vault ({len(req.cookies)} cookies)")
        except Exception as e:
            logger.warning(f"persist-session: cookie save error: {e}")

    # 2. Save Chrome profile tar to Drive FUSE — canonical path: profiles/PRFL-NNN.tar.gz
    if req.profile_dir and os.path.isdir(req.profile_dir):
        try:
            # Determine canonical profile number: resolve via XIOSYNC identity-internal
            # API first (authoritative DB source), fall back to Drive scan only as last resort.
            _profiles_dir_ps = os.path.join(d_root, "profiles")
            os.makedirs(_profiles_dir_ps, exist_ok=True)
            _resolved_serial = None
            if x_base and req.email:
                for _retry_ps in range(2):
                    try:
                        import httpx as _hx_ps

                        _idr = _hx_ps.get(
                            f"{x_base}/api/v1/xioflow/events/identity-internal",
                            params={"email": req.email, "platform": "google"},
                            headers={
                                "X-XIOSYNC-Internal": os.environ.get(
                                    "XIORUN_INTERNAL_SECRET",
                                    os.environ.get("XIOSYNC_INTERNAL_SECRET", ""),
                                )
                            },
                            timeout=10,
                        )
                        if _idr.status_code == 200:
                            _idata = _idr.json()
                            _resolved_serial = _idata.get("profile_serial")
                            logger.info(
                                f"persist-session: identity resolved → PRFL-{_resolved_serial:03d}"
                            )
                            break
                        else:
                            logger.warning(
                                f"persist-session: identity-internal {_idr.status_code} — falling back to Drive scan"
                            )
                            break
                    except Exception as _ie_ps:
                        logger.warning(
                            f"persist-session: identity resolve attempt {_retry_ps + 1} failed: {_ie_ps}"
                        )
                        if _retry_ps == 0:
                            time.sleep(2)

            # Drive fingerprint.json fallback — scan for fingerprint with matching identity_id
            if not _resolved_serial and req.identity_id and os.path.isdir(_profiles_dir_ps):
                for _fp_fn in sorted(os.listdir(_profiles_dir_ps)):
                    if _fp_fn.endswith(".fingerprint.json") and _fp_fn.startswith("PRFL-"):
                        try:
                            _fp_path_scan = os.path.join(_profiles_dir_ps, _fp_fn)
                            with open(_fp_path_scan) as _fp_f_s:
                                _fp_d = json.load(_fp_f_s)
                            if _fp_d.get("identity_id", "").lower() == req.identity_id.lower():
                                _prfl_id_str = _fp_d.get("profile_id", "")
                                _resolved_serial = (
                                    int(_prfl_id_str.split("-")[1]) if _prfl_id_str else None
                                )
                                if _resolved_serial:
                                    logger.info(
                                        f"persist-session: identity resolved from Drive fingerprint → PRFL-{_resolved_serial:03d}"
                                    )
                                    break
                        except Exception:
                            pass

            if _resolved_serial:
                _next_num = _resolved_serial
            else:
                _existing_nums = []
                if os.path.isdir(_profiles_dir_ps):
                    for _pf in sorted(os.listdir(_profiles_dir_ps)):
                        if _pf.endswith(".tar.gz") and _pf.startswith("PRFL-"):
                            try:
                                _existing_nums.append(int(_pf[5:8]))
                            except ValueError:
                                pass
                _next_num = (max(_existing_nums) + 1) if _existing_nums else 1
                logger.warning(
                    f"persist-session: using fallback PRFL-{_next_num:03d} (identity-resolve unavailable)"
                )

            _profile_key = f"profiles/PRFL-{_next_num:03d}.tar.gz"
            _drive_target_path = os.path.join(d_root, _profile_key)

            # Trim cache dirs before archiving
            _TRIM = [
                "Cache",
                "Code Cache",
                "GPUCache",
                "DawnCache",
                "ShaderCache",
                "Service Worker/CacheStorage",
                "Service Worker/ScriptCache",
                "BudgetDatabase",
                "Network Action Predictor",
            ]
            for _td in _TRIM:
                _full = os.path.join(req.profile_dir, _td)
                if os.path.exists(_full):
                    _sh_ps.rmtree(_full, ignore_errors=True)

            _tmp_tar = _tmp_ps.NamedTemporaryFile(suffix=".tar.gz", delete=False)
            _tmp_tar_path = _tmp_tar.name
            _tmp_tar.close()
            try:
                with _tf_cc.open(_tmp_tar_path, "w:gz") as tf:
                    tf.add(req.profile_dir, arcname=os.path.basename(req.profile_dir))
                _sz = os.path.getsize(_tmp_tar_path)
                _sh_ps.copy2(_tmp_tar_path, _drive_target_path)
                results["profile_saved"] = True
                results["profile_key"] = _profile_key
                results["profile_size"] = _sz
                logger.info(f"persist-session: profile saved to Drive: {_profile_key} ({_sz:,}b)")

                # ── Write PRFL-NNN.fingerprint.json ──────────────────────────────
                try:
                    _fp_data = {
                        "profile_id": f"PRFL-{_next_num:03d}",
                        "identity_id": req.identity_id or "",
                        "email": getattr(req, "email", "") or "",
                        "platform": "google",
                        "profile_dir": req.profile_dir or "",
                        "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
                        "cookie_count": results.get("cookie_count", 0),
                        "node": n_name,
                    }
                    _fp_path = os.path.join(
                        d_root, "profiles", f"PRFL-{_next_num:03d}.fingerprint.json"
                    )
                    with open(_fp_path, "w") as _fp_f:
                        json.dump(_fp_data, _fp_f, indent=2)
                    logger.info(f"persist-session: fingerprint written → {_fp_path}")
                    results["fingerprint_key"] = f"profiles/PRFL-{_next_num:03d}.fingerprint.json"
                except Exception as _fp_e:
                    logger.warning(f"persist-session: fingerprint write failed: {_fp_e}")

                # Clean up legacy chrome_profiles/ duplicate if it exists
                _id_short_ps = req.identity_id.replace("-", "")[:16] if req.identity_id else ""
                _legacy_key = f"chrome_profiles/PRFL_{_id_short_ps}.tar.gz"
                _legacy_path = os.path.join(d_root, _legacy_key)
                if os.path.exists(_legacy_path):
                    try:
                        os.unlink(_legacy_path)
                        logger.info(f"persist-session: removed legacy duplicate {_legacy_key}")
                    except Exception as _le:
                        logger.warning(f"persist-session: legacy cleanup failed: {_le}")
            finally:
                os.unlink(_tmp_tar_path)
        except Exception as e:
            logger.warning(f"persist-session: profile save error: {e}")

    return results
