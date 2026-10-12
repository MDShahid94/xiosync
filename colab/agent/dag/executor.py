"""XIOFlow DAG execution engine using local Patchright."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger("xiorun_agent")

try:
    from agent.dag.retry import _dom_retry_backoff
except ImportError:
    try:
        from agent.dag.retry import _dom_retry_backoff
    except ImportError:
        async def _dom_retry_backoff(coro_fn, *, max_retries: int = 3, base_delay: float = 1.0, label: str = "dom_action"):
            _delay = base_delay
            for _attempt in range(max_retries + 1):
                try:
                    return await coro_fn()
                except Exception:
                    if _attempt == max_retries:
                        raise
                    await asyncio.sleep(_delay)
                    _delay = min(_delay * 2, 10.0)

try:
    from agent.dag.handlers.google import _DAG_POST_ACTION_HANDLERS
except ImportError:
    try:
        from agent.dag.handlers.google import _DAG_POST_ACTION_HANDLERS
    except ImportError:
        _DAG_POST_ACTION_HANDLERS = {}

_SSH_PROXY_URL: str | None = None


def get_ssh_proxy_url() -> str | None:
    global _SSH_PROXY_URL
    if _SSH_PROXY_URL:
        return _SSH_PROXY_URL
    try:
        from agent.server.lifespan import get_ssh_proxy_url as _g
        return _g()
    except ImportError:
        try:
            from agent.server.lifespan import get_ssh_proxy_url as _g
            return _g()
        except ImportError:
            return os.environ.get("SSH_PROXY_URL")


async def _execute_dag_run(
    run_id: str,
    task_id: str | None,
    dag_domain: str,
    dag_root_intent: str,
    context: dict,
) -> None:
    """Execute a DAG run claimed from XIOSYNC using local Patchright."""
    _XIOSYNC = os.environ.get("XIORUN_XIOSYNC_BASE", os.environ.get("XIOSYNC_BASE", ""))
    _SECRET = os.environ.get(
        "XIORUN_INTERNAL_SECRET", os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    )
    _HDRS = {"X-XIOSYNC-Internal": _SECRET, "Content-Type": "application/json"}
    _trace = context.get("trace_mode", False)
    _trace_requested = _trace  # original flag — never overridden, drives trace_nodes collection
    _vars = dict(context.get("workflow_vars", {}))  # mutable copy
    # Merge top-level context fallback keys — so dispatchers can put email/identity_id
    # at the context root OR inside workflow_vars and both patterns work.
    for _ck in (
        "email",
        "identity_id",
        "persist_mode",
        "organization_id",
        "password",
        "totp_secret",
        "proxy_url",
    ):
        if _ck not in _vars and _ck in context:
            _vars[_ck] = context[_ck]
    _proxy = get_ssh_proxy_url()  # socks5://127.0.0.1:19055 (or 19056)

    logger.info(
        f"dag_run.start: run={run_id} domain={dag_domain} intent={dag_root_intent} trace={_trace}"
    )

    try:
        import httpx as _hx
        from patchright.async_api import async_playwright as _pw

        # 1. Fetch DAG graph from XIOSYNC
        async with _hx.AsyncClient(timeout=30) as _cl:
            _gr = await _cl.get(
                f"{_XIOSYNC}/api/v1/xioflow/events/memory-graph-internal",
                params={"domain": dag_domain, "intent": dag_root_intent},
                headers=_HDRS,
            )
            if _gr.status_code != 200:
                raise RuntimeError(f"graph fetch {_gr.status_code}: {_gr.text[:200]}")
            _graph = _gr.json()

        nodes = _graph.get("nodes", [])
        logger.info(f"dag_run.graph: {len(nodes)} nodes for {dag_domain}/{dag_root_intent}")

        # ── P0-5: Distributed advisory lock (per email) ─────────────────────
        # Prevents two workers from signing into the same account simultaneously.
        _email = _vars.get("email", "")
        _persist_mode = _vars.get("persist_mode", "tarball+vault")
        _org_id = context.get("organization_id", "00000000-0000-7000-8000-000000000000")
        _iid_from_vars = _vars.get("identity_id", "")
        _lock_acquired = False
        _lock_key = None
        _lock_conn = None

        if _email:
            try:
                import hashlib as _hl

                _lock_key = abs(int(_hl.sha256(_email.encode()).hexdigest()[:8], 16)) % (2**31 - 1)
                import psycopg as _pg

                _lock_conn = await _pg.AsyncConnection.connect(
                    os.environ.get(
                        "DATABASE_URL", "postgresql://xiosync:xiosync@localhost:5432/xiosync"
                    ),
                    autocommit=True,
                )
                _lrow = await _lock_conn.execute(f"SELECT pg_try_advisory_lock({_lock_key})")
                _lock_result = (await _lrow.fetchone())[0]
                if _lock_result:
                    _lock_acquired = True
                    logger.info(
                        f"dag_run.lock: acquired advisory lock for '{_email}' (key={_lock_key})"
                    )
                else:
                    logger.warning(
                        f"dag_run.lock: another worker holds lock for '{_email}' — proceeding anyway"
                    )
            except Exception as _le:
                logger.info(f"dag_run.lock: advisory lock skipped ({_le})")
                _lock_conn = None

        _launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1920,1080",
            "--disable-blink-features=AutomationControlled",
            "--lang=en-US",
        ]
        if _proxy:
            _launch_args.append(f"--proxy-server={_proxy}")

        _TIER_FN = {
            1: lambda pv: f'[data-testid="{pv["test_id"]}"]' if pv.get("test_id") else None,
            2: lambda pv: f'[aria-label="{pv["aria_label"]}"]' if pv.get("aria_label") else None,
            3: lambda pv: pv.get("axes_xpath"),
            4: lambda pv: f'text="{pv["inner_text"]}"' if pv.get("inner_text") else None,
            5: lambda pv: f"xpath={pv['xpath']}" if pv.get("xpath") else None,
            6: lambda pv: pv.get("css"),
        }

        results = []  # summary per step
        trace_nodes = []  # rich nodes for trace_mode=True deploy
        all_ok = True

        async with _pw() as pw:
            # Use pinned /opt/chrome131/chrome if available — avoids patchright version
            # mismatch when patchright updates its bundled chromium build number.
            _chrome_bin = "/opt/chrome131/chrome"
            _launch_kwargs: dict = {"headless": False, "args": _launch_args}
            if os.path.isfile(_chrome_bin):
                _launch_kwargs["executable_path"] = _chrome_bin
                logger.info(f"dag_run: using pinned Chrome at {_chrome_bin}")
            _brow = await pw.chromium.launch(**_launch_kwargs)

            # ── P0-1/P0-2: Identity-scoped profile restore (tarball + storage_state) ──
            # Key by PRFL-NNN, not domain slug, to avoid multi-account collisions.
            _DRIVE_ROOT = "/content/drive/MyDrive/XIOSYNC-Shared"
            _node_slug = os.environ.get("WORKER_NODE", "worker")[:16]
            _profile_serial = 0  # resolved below
            _user_data_dir = None  # set if tarball was restored
            _st_path = None  # resolved below (identity-scoped)

            # Step A: resolve profile_serial via XIOSYNC internal API (not local DB)
            try:
                async with _hx.AsyncClient(timeout=10) as _cl:
                    _ir = await _cl.get(
                        f"{_XIOSYNC}/api/v1/xioflow/events/identity-internal",
                        params={"email": _email, "platform": "google"},
                        headers=_HDRS,
                    )
                _id_data = _ir.json()
                if _id_data.get("found"):
                    _iid_resolved = _id_data["identity_id"]
                    _profile_serial = int(_id_data.get("profile_serial") or 0)
                    logger.info(
                        f"dag_run.profile: identity resolved → PRFL-{_profile_serial:03d} (id={_iid_resolved[:8]})"
                    )
                else:
                    _iid_resolved = _iid_from_vars
                    logger.info(
                        "dag_run.profile: identity not found in DB yet — will auto-create on persist"
                    )
            except Exception as _re:
                _iid_resolved = _iid_from_vars
                logger.info(f"dag_run.profile: identity API lookup skipped ({_re})")

            # Identity-scoped storage_state path (PRFL-NNN or domain-slug fallback)
            _ss_dir = os.path.join(_DRIVE_ROOT, "storage_states")
            if _profile_serial:
                _st_path = os.path.join(_ss_dir, f"PRFL-{_profile_serial:03d}_storage_state.json")
                # migrate legacy domain-slug file if exists and PRFL file doesn't
                _legacy_slug = dag_domain.replace(".", "_").replace("/", "_")
                _legacy_path = os.path.join(_ss_dir, f"{_legacy_slug}_storage_state.json")
                if not os.path.isfile(_st_path) and os.path.isfile(_legacy_path):
                    import shutil as _shu

                    _shu.copy2(_legacy_path, _st_path)
                    logger.info(f"dag_run.profile: migrated legacy storage_state → {_st_path}")
            else:
                _legacy_slug = dag_domain.replace(".", "_").replace("/", "_")
                _st_path = os.path.join(_ss_dir, f"{_legacy_slug}_storage_state.json")

            # Step B: try tarball restore (persist_mode = tarball+vault)
            if _persist_mode in ("tarball+vault", "tarball_only") and _profile_serial:
                try:
                    import tarfile as _tf

                    _tar_src = os.path.join(
                        _DRIVE_ROOT, "profiles", f"PRFL-{_profile_serial:03d}.tar.gz"
                    )
                    _local_profile_root = (
                        f"/tmp/xiorun_profiles/PRFL-{_profile_serial:03d}__{_node_slug}"
                    )
                    if os.path.isfile(_tar_src) and not os.path.isdir(_local_profile_root):
                        os.makedirs("/tmp/xiorun_profiles", exist_ok=True)
                        with _tf.open(_tar_src, "r:gz") as _tf_obj:
                            _tf_obj.extractall("/tmp/xiorun_profiles")
                        # find extracted dir (may be named differently inside tar)
                        _extracted = [
                            os.path.join("/tmp/xiorun_profiles", d)
                            for d in os.listdir("/tmp/xiorun_profiles")
                            if d.startswith("PRFL-") or d.startswith("xiorun_profiles")
                        ]
                        if _extracted:
                            import shutil as _shu2

                            _shu2.move(_extracted[0], _local_profile_root)
                    if os.path.isdir(_local_profile_root):
                        _user_data_dir = _local_profile_root
                        logger.info(f"dag_run.profile: tarball restored → {_user_data_dir}")
                except Exception as _te:
                    logger.info(f"dag_run.profile: tarball restore skipped ({_te})")

            # Step C: build browser context
            _ctx_kwargs: dict = {
                "viewport": {"width": 1920, "height": 1080},
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "locale": "en-US",
            }
            if _user_data_dir:
                # Tarball available — use persistent context (full device state)
                await _brow.close()  # don't need the ephemeral browser
                _bctx = await pw.chromium.launch_persistent_context(
                    _user_data_dir,
                    headless=False,
                    args=_launch_args,
                    **({("executable_path"): _chrome_bin} if os.path.isfile(_chrome_bin) else {}),
                    **_ctx_kwargs,
                )
                _brow = None  # persistent context is its own browser
                _page = _bctx.pages[0] if _bctx.pages else await _bctx.new_page()
                logger.info(
                    f"dag_run.profile: launched with persistent user_data_dir={_user_data_dir}"
                )
            else:
                # No tarball — fall back to ephemeral context + cookie injection
                if _st_path and os.path.isfile(_st_path):
                    _ctx_kwargs["storage_state"] = _st_path
                    logger.info(f"dag_run.profile: loading storage_state from {_st_path}")
                _bctx = await _brow.new_context(**_ctx_kwargs)
                _page = await _bctx.new_page()

            # Optional: wrap with PageProxy for auto-trace (best-effort; Colab workers skip)
            if _trace:
                try:
                    from xiosync.subsystems.xioflow.ingestion.page_proxy import PageProxy
                    from xiosync.subsystems.xioflow.ingestion.trace_collector import TraceCollector

                    _tc = TraceCollector(org_id="internal", domain=dag_domain, run_id=run_id)
                    _page = PageProxy(_page, _tc)
                    logger.info("dag_run.trace: PageProxy active")
                except Exception as _te:
                    logger.info(
                        f"dag_run.trace: PageProxy unavailable ({_te}), using graph-based trace"
                    )
                    _trace = False  # disable PageProxy wrapping only
                    _tc = None
            else:
                _tc = None

            _uc_succeeded = False  # set True by phase1_uc_stealth_login on success
            for _node in nodes:
                _intent = _node.get("intent", "?")
                _action = _node.get("action_type", "")
                _params = _node.get("action_params", {})
                _pv = _node.get("place_value") or {}
                _prio = _node.get("locator_priority") or [6, 1, 2, 3, 4, 5, 7]
                _step_ok = False

                try:
                    # Skip native patchright nodes when UC login already succeeded.
                    # Script nodes (phase2_navigate_to_gmail, phase3_verify, phase3_5_persist_session)
                    # must still run — they use xiorun_agent HTTP endpoints, not patchright browser.
                    _patchright_native = _action in ("navigate", "fill", "click", "wait")
                    if _uc_succeeded and _patchright_native:
                        logger.info(
                            f"dag_run.step: {_intent} [{_action}] skipped (UC login already succeeded)"
                        )
                        _step_ok = True
                    elif _action == "navigate":
                        _url = _params.get("url", "")
                        for _k, _v in _vars.items():
                            _url = _url.replace(f"{{{_k}}}", str(_v))
                        await _dom_retry_backoff(
                            lambda: _page.goto(_url, wait_until="domcontentloaded", timeout=30000),
                            max_retries=2,
                            base_delay=2.0,
                            label=f"navigate:{_intent}",
                        )
                        await _page.wait_for_timeout(2000)
                        _step_ok = True
                        _final_url = _page.url
                        logger.info(
                            f"dag_run.step: {_intent} navigate → {_url[:60]} (final={_final_url[:60]})"
                        )

                        # ── Pre-flight auth check (mirrors mjs Phase 0) ────────────────
                        # If navigate_to_signin was redirected away from the signin page,
                        # the stored cookies are valid → user is already authenticated.
                        # Short-circuit: skip all remaining nodes, go straight to persist.
                        if _intent == "navigate_to_signin" and (
                            "myaccount.google.com" in _final_url
                            or (
                                "accounts.google.com" not in _final_url
                                and "signin" not in _final_url
                                and "identifier" not in _final_url
                            )
                        ):
                            logger.info(
                                f"dag_run.preflight: already authenticated → "
                                f"{_final_url[:70]} — skipping signin DAG"
                            )
                            results.append(
                                {"intent": _intent, "ok": True, "note": "already_authenticated"}
                            )
                            all_ok = True
                            break  # exit node loop → go directly to storage_state save + vault

                    elif _action in ("fill", "type"):
                        # "value" key (new nodes) OR "text" key (legacy)
                        _text = _params.get("value", _params.get("text", ""))
                        # ── Resolve {{var}} tokens ─────────────────────────────────────
                        if "{{totp}}" in str(_text):
                            _totp_sec = (
                                (_params.get("totp_secret") or _vars.get("totp_secret", ""))
                                .replace(" ", "")
                                .replace("-", "")
                            )
                            try:
                                import pyotp as _pt

                                _text = _pt.TOTP(_totp_sec).now()
                                logger.info(f"dag_run.step: {_intent} TOTP generated ({_text})")
                            except Exception as _te:
                                logger.warning(f"dag_run.step: {_intent} TOTP fail: {_te}")
                        else:
                            # Generic {{key}} → workflow_vars lookup
                            for _k, _v in _vars.items():
                                _text = str(_text).replace(f"{{{{{_k}}}}}", str(_v))
                        # ── Smart wait: wait for the target field to appear (handles page transitions) ──
                        _waited = False
                        for _wt in _prio:
                            _wfn = _TIER_FN.get(_wt)
                            if not _wfn:
                                continue
                            _wls = _wfn(_pv)
                            if not _wls:
                                continue
                            try:
                                await _page.wait_for_selector(_wls, timeout=10000, state="visible")
                                _waited = True
                                break
                            except Exception:
                                continue
                        if not _waited:
                            await _page.wait_for_timeout(2000)
                        for _t in _prio:
                            _fn = _TIER_FN.get(_t)
                            if not _fn:
                                continue
                            _ls = _fn(_pv)
                            if not _ls:
                                continue
                            try:
                                _loc = _page.locator(_ls)
                                if await _loc.count() == 0:
                                    continue
                                await _loc.first.fill(_text, timeout=8000)
                                _step_ok = True
                                _step_rec = {
                                    "intent": _intent,
                                    "action": _action,
                                    "ok": True,
                                    "locator_tier": _t,
                                    "locator_str": _ls,
                                }
                                logger.info(
                                    f"dag_run.step: {_intent} fill T{_t} {_ls!r} val={repr(_text[:20])}"
                                )
                                break
                            except Exception:
                                continue

                    elif _action == "click":
                        for _t in _prio:
                            _fn = _TIER_FN.get(_t)
                            if not _fn:
                                continue
                            _ls = _fn(_pv)
                            if not _ls:
                                continue
                            try:
                                _loc = _page.locator(_ls)
                                if await _loc.count() == 0:
                                    continue
                                await _loc.first.click(timeout=8000)
                                _step_ok = True
                                _step_rec = {
                                    "intent": _intent,
                                    "action": _action,
                                    "ok": True,
                                    "locator_tier": _t,
                                    "locator_str": _ls,
                                }
                                logger.info(f"dag_run.step: {_intent} click T{_t} {_ls!r}")
                                break
                            except Exception:
                                continue

                    elif _action == "wait":
                        await _page.wait_for_timeout(_params.get("ms", 2000))
                        _step_ok = True

                    elif _action == "script":
                        # ── P1: Script node runner ────────────────────────────────────────
                        # Nodes with endpoint= call the worker's own local HTTP API.
                        # Response JSON is injected into _vars for downstream var resolution.
                        _s_endpoint = _params.get("endpoint", "")

                        if _intent == "resolve_exit_proxy":
                            # ── Dynamic proxy resolution ────────────────────────
                            # Priority: 0) Pre-set proxy_url in vars → 1) PPPoE → 2) SSH SOCKS5
                            import subprocess as _sp_proxy  # ensure always available
                            _resolved_proxy = None
                            _resolved_ip = None
                            _xio_base = os.environ.get("XIORUN_XIOSYNC_BASE", "")
                            _xio_secret = os.environ.get("XIORUN_INTERNAL_SECRET", "")

                            # ── 0. Use pre-set proxy if already in workflow vars ─
                            _preset_proxy = _vars.get("proxy_url", "")
                            if _preset_proxy:
                                try:
                                    _t0 = _sp_proxy.run(
                                        ["curl", "-s", "--max-time", "6", "--proxy",
                                         _preset_proxy, "https://accounts.google.com",
                                         "-o", "/dev/null", "-w", "%{http_code}"],
                                        capture_output=True, text=True, timeout=10,
                                    )
                                    if _t0.stdout.strip() not in ("", "000"):
                                        _resolved_proxy = _preset_proxy
                                        logger.info(
                                            f"resolve_exit_proxy: pre-set proxy OK "
                                            f"{_preset_proxy} (http={_t0.stdout.strip()})"
                                        )
                                except Exception as _t0e:
                                    logger.info(f"resolve_exit_proxy: pre-set proxy test failed: {_t0e}")

                            # ── 1. Try PPPoE exit nodes via XIOSYNC API ────────
                            if _xio_base and _xio_secret:
                                try:
                                    import urllib.request as _urq_proxy
                                    import json as _json_proxy
                                    _req = _urq_proxy.Request(
                                        f"{_xio_base}/api/v1/pppoe/nodes",
                                        headers={"X-XIOSYNC-Internal": _xio_secret},
                                    )
                                    _resp = _urq_proxy.urlopen(_req, timeout=10)
                                    _nodes = _json_proxy.loads(_resp.read()).get("nodes", [])
                                    _idle = [
                                        n for n in _nodes
                                        if n.get("state") == "idle"
                                        and n.get("proxy_state") == "running"
                                    ]
                                    logger.info(
                                        f"resolve_exit_proxy: {len(_idle)} idle PPPoE slots"
                                    )
                                    # Test up to 5 idle slots for Google reachability
                                    for _slot in _idle[:5]:
                                        _p_url = _slot.get("proxy_url", "")
                                        _p_ip = _slot.get("public_ip", "")
                                        try:
                                            _test = _sp_proxy.run(
                                                [
                                                    "curl", "-s", "--max-time", "6",
                                                    "--proxy", _p_url,
                                                    "https://accounts.google.com",
                                                    "-o", "/dev/null", "-w", "%{http_code}",
                                                ],
                                                capture_output=True, text=True, timeout=10,
                                            )
                                            if _test.stdout.strip() not in ("", "000"):
                                                _resolved_proxy = _p_url
                                                _resolved_ip = _p_ip
                                                logger.info(
                                                    f"resolve_exit_proxy: PPPoE slot "
                                                    f"{_slot.get('slot')} OK → {_p_url}"
                                                )
                                                break
                                            else:
                                                logger.debug(
                                                    f"resolve_exit_proxy: PPPoE slot "
                                                    f"{_slot.get('slot')} dead"
                                                )
                                        except Exception:
                                            pass
                                except Exception as _pex:
                                    logger.warning(
                                        f"resolve_exit_proxy: PPPoE fetch failed: {_pex}"
                                    )

                            # ── 2. Fallback: SSH SOCKS5 tunnel ─────────────────
                            if not _resolved_proxy:
                                try:
                                    from agent.config import PROXY_LOCAL_PORT as _proxy_port
                                except Exception:
                                    _proxy_port = int(os.environ.get("XIORUN_PROXY_LOCAL_PORT", "19056"))
                                _ssh_proxy = os.environ.get(
                                    "XIORUN_PROXY",
                                    os.environ.get(
                                        "PROXY_URL",
                                        f"socks5://127.0.0.1:{_proxy_port}",
                                    ),
                                )
                                # Verify SSH proxy is alive
                                try:
                                    _test2 = _sp_proxy.run(
                                        [
                                            "curl", "-s", "--max-time", "8",
                                            "--proxy", _ssh_proxy,
                                            "https://accounts.google.com",
                                            "-o", "/dev/null", "-w", "%{http_code}",
                                        ],
                                        capture_output=True, text=True, timeout=12,
                                    )
                                    if _test2.stdout.strip() not in ("", "000"):
                                        _resolved_proxy = _ssh_proxy
                                        # Get public IP through SSH tunnel
                                        try:
                                            _ip_r = _sp_proxy.run(
                                                [
                                                    "curl", "-s", "--max-time", "5",
                                                    "--proxy", _ssh_proxy,
                                                    "https://api.ipify.org",
                                                ],
                                                capture_output=True, text=True, timeout=8,
                                            )
                                            _resolved_ip = _ip_r.stdout.strip()
                                        except Exception:
                                            pass
                                        logger.info(
                                            f"resolve_exit_proxy: SSH fallback OK → "
                                            f"{_ssh_proxy} (ip={_resolved_ip})"
                                        )
                                    else:
                                        logger.error(
                                            "resolve_exit_proxy: SSH proxy unreachable"
                                        )
                                except Exception as _sex:
                                    logger.error(
                                        f"resolve_exit_proxy: SSH proxy test failed: {_sex}"
                                    )

                            if _resolved_proxy:
                                _vars["proxy_url"] = _resolved_proxy
                                if _resolved_ip:
                                    _vars["exit_node_public_ip"] = _resolved_ip
                                _step_ok = True
                                logger.info(
                                    f"dag_run.step: {_intent} [script] "
                                    f"proxy={_resolved_proxy} ip={_resolved_ip}"
                                )
                            else:
                                logger.error(
                                    "dag_run.step: resolve_exit_proxy FAILED — "
                                    "no PPPoE or SSH proxy reachable"
                                )
                                _step_ok = False

                        elif _s_endpoint:
                            # Resolve {{var}} tokens in ALL params
                            _s_body: dict = {}
                            for _spk, _spv in _params.items():
                                _s_val = str(_spv)
                                for _k, _v in _vars.items():
                                    _s_val = _s_val.replace(f"{{{{{_k}}}}}", str(_v))
                                _s_body[_spk] = _s_val

                            _worker_port = os.environ.get("PORT", "9300")
                            _worker_url = f"http://127.0.0.1:{_worker_port}{_s_endpoint}"
                            try:
                                # run-uc-login has HITL (up to 600s) + login (120s) = 900s
                                _script_timeout = 900 if _s_endpoint == "/run-uc-login" else 180
                                async with _hx.AsyncClient(timeout=_script_timeout) as _cl:
                                    _sr = await _cl.post(_worker_url, json=_s_body)
                                _ct = _sr.headers.get("content-type", "")
                                _sd = _sr.json() if "application/json" in _ct else {}
                                if _sr.status_code in (200, 201):
                                    _step_ok = True
                                    # Inject response fields into _vars for downstream nodes
                                    if isinstance(_sd, dict):
                                        for _rk, _rv in _sd.items():
                                            _vars[_rk] = _rv
                                    logger.info(
                                        f"dag_run.step: {_intent} [script] {_s_endpoint} "
                                        f"→ HTTP {_sr.status_code} ok  keys={list(_sd.keys() if isinstance(_sd, dict) else [])}"
                                    )
                                else:
                                    logger.warning(
                                        f"dag_run.step: {_intent} [script] {_s_endpoint} "
                                        f"→ HTTP {_sr.status_code} {str(_sd)[:120]}"
                                    )
                                    _step_ok = False
                            except Exception as _sce:
                                logger.warning(
                                    f"dag_run.step: {_intent} [script] {_s_endpoint} error: {_sce}"
                                )
                                _step_ok = False

                        else:
                            # No endpoint + no known handler — skip with warning
                            logger.warning(
                                f"dag_run.step: {_intent} [script] no endpoint — skipping"
                            )
                            _step_ok = True

                        # ── Post-script early-exit checks ─────────────────────────
                        if _step_ok:
                            if _intent == "phase0_cascade_check":
                                # Cascade check says session is already valid — skip main DAG
                                if _vars.get("valid") is True or _vars.get("valid") == "true":
                                    logger.info(
                                        "dag_run.script: phase0 session valid → "
                                        f"skipping DAG  profile_dir={_vars.get('profile_dir', '?')}"
                                    )
                                    results.append(
                                        {"intent": _intent, "ok": True, "note": "cascade_valid"}
                                    )
                                    all_ok = True
                                    break
                                else:
                                    logger.info(
                                        "dag_run.script: phase0 session invalid → "
                                        "continuing to phase1 UC login"
                                    )

                            elif _intent == "phase1_uc_stealth_login":
                                # UC login succeeded — session fully established.
                                # Set a flag so the native patchright re-login nodes
                                # (navigate_to_signin, type_email, type_password, click_signin,
                                # verify_signed_in) are skipped, but script nodes
                                # (phase2_navigate_to_gmail, phase3_verify, phase3_5_persist_session)
                                # still execute normally.
                                if _vars.get("ok") is True or _vars.get("success") is True:
                                    logger.info(
                                        "dag_run.script: phase1 UC login success → "
                                        "skipping patchright re-login nodes"
                                    )
                                    _uc_succeeded = True
                                # If UC login failed, fall through to native patchright nodes.

                    else:
                        logger.warning(
                            f"dag_run.step: unknown action {_action!r} for {_intent!r} — skipping"
                        )
                        _step_ok = True

                    # ── Post-action handlers (domain-agnostic registry) ──────
                    # Dispatches registered handlers for this (domain, intent)
                    # pair. E.g. Google challenge/post-login handlers fire after
                    # click_signin on accounts.google.com, but any domain can
                    # register its own handlers without modifying this runner.
                    _post_handlers = _DAG_POST_ACTION_HANDLERS.get((dag_domain, _intent), [])
                    if _step_ok and _post_handlers:
                        for _ph in _post_handlers:
                            await _ph(_page, _vars, _XIOSYNC, _HDRS, run_id, logger)

                except Exception as _se:
                    logger.error(f"dag_run.step_error: {_intent} [{_action}] {_se}")
                    _step_ok = False

                _step_rec = {
                    "intent": _intent,
                    "action": _action,
                    "ok": _step_ok,
                    "locator_tier": None,
                    "locator_str": None,
                }
                results.append(_step_rec)
                # Record rich node data for trace deployment
                if _trace_requested:
                    trace_nodes.append(
                        {
                            "intent": _intent,
                            "action_type": _action,
                            "place_value": _pv or {},
                            "action_params": _params or {},
                            "previous_intent": results[-2]["intent"] if len(results) >= 2 else None,
                        }
                    )
                if not _step_ok and _action != "wait":
                    # Check if this step is marked fail_ok (non-fatal) in action_params
                    _fail_ok = bool((_params or {}).get("fail_ok", False))
                    if _fail_ok:
                        logger.warning(
                            f"dag_run: step {_intent!r} failed (fail_ok=True) — continuing DAG"
                        )
                        # Special case: navigate_to_signin fail_ok + Phase 0 already confirmed auth.
                        # If UC login returned final_url pointing to myaccount (Phase 0 shortcut),
                        # the user is authenticated — short-circuit exactly like the preflight redirect does.
                        if _intent == "navigate_to_signin":
                            _uc_final = _vars.get("final_url", "")
                            if (
                                _uc_final
                                and "accounts.google.com" not in _uc_final
                                and "signin" not in _uc_final
                            ):
                                logger.info(
                                    f"dag_run.preflight: phase0 auth confirmed → {_uc_final[:60]} "
                                    f"— shortcircuiting DAG (SOCKS drop on navigate_to_signin)"
                                )
                                all_ok = True
                                break  # skip type_email, type_password, etc.
                    else:
                        all_ok = False
                        logger.warning(f"dag_run: step {_intent!r} failed — stopping DAG")
                        break

                await _page.wait_for_timeout(1500)

            # ── Profile persistence (P0-1/P0-2) ─────────────────────────────────────
            if all_ok:
                _st = None
                try:
                    _st = await _bctx.storage_state()
                    os.makedirs(_ss_dir, exist_ok=True)
                    import json as _stj

                    with open(_st_path, "w") as _stf:
                        _stj.dump(_st, _stf)
                    logger.info(
                        f"dag_run.profile: storage_state saved → {_st_path} "
                        f"({len(_st.get('cookies', []))} cookies)"
                    )
                except Exception as _pe:
                    logger.debug(f"dag_run.profile: storage_state save skipped ({_pe})")

                # P0-1: tarball save — pack user_data_dir → PRFL-NNN.tar.gz on Drive
                if (
                    _persist_mode in ("tarball+vault", "tarball_only")
                    and _user_data_dir
                    and _profile_serial
                ):
                    try:
                        import shutil as _shf
                        import tarfile as _tfw
                        import tempfile as _tmpf

                        _TRIM_DIRS = [
                            "Cache",
                            "Code Cache",
                            "GPUCache",
                            "DawnCache",
                            "ShaderCache",
                            "BudgetDatabase",
                            "Network Action Predictor",
                            "Service Worker/CacheStorage",
                            "Service Worker/ScriptCache",
                        ]
                        for _td in _TRIM_DIRS:
                            _fp = os.path.join(_user_data_dir, _td)
                            if os.path.exists(_fp):
                                _shf.rmtree(_fp, ignore_errors=True)
                        _tar_dst = os.path.join(
                            _DRIVE_ROOT, "profiles", f"PRFL-{_profile_serial:03d}.tar.gz"
                        )
                        os.makedirs(os.path.dirname(_tar_dst), exist_ok=True)
                        _tmp_tar = _tmpf.NamedTemporaryFile(suffix=".tar.gz", delete=False)
                        _tmp_tar.close()
                        with _tfw.open(_tmp_tar.name, "w:gz") as _tfw_obj:
                            _tfw_obj.add(_user_data_dir, arcname=os.path.basename(_user_data_dir))
                        _shf.copy2(_tmp_tar.name, _tar_dst)
                        os.unlink(_tmp_tar.name)
                        _tar_sz = os.path.getsize(_tar_dst)
                        logger.info(
                            f"dag_run.profile: tarball saved → {_tar_dst} ({_tar_sz // 1024}KB)"
                        )
                    except Exception as _tse:
                        logger.warning(f"dag_run.profile: tarball save failed ({_tse})")

                # Vault save: AES-GCM encrypt cookies → vaulted_secrets via XIOSYNC
                if _st:
                    try:
                        import asyncio as _aio

                        async with _hx.AsyncClient(timeout=8) as _cl:
                            _pr = await _aio.wait_for(
                                _cl.post(
                                    f"{_XIOSYNC}/api/v1/xioflow/events/runs-internal/{run_id}/persist-session",
                                    headers=_HDRS,
                                    json={
                                        "storage_state": _st,
                                        "email": _email,
                                        "identity_id": _iid_resolved,
                                        "org_id": _org_id,
                                    },
                                ),
                                timeout=8,
                            )
                        _pd = _pr.json()
                        _serial_v = _pd.get("profile_serial", 0)
                        logger.info(
                            f"dag_run.profile: vault saved → identity={str(_pd.get('identity_id', '?'))[:8]} "
                            f"PRFL-{_serial_v:03d} ({_pd.get('cookie_count', 0)} cookies encrypted)"
                        )
                    except BaseException as _ve:
                        logger.warning(
                            f"dag_run.profile: vault save failed ({type(_ve).__name__}: {_ve})"
                        )
                        # Do NOT re-raise — vault failure is non-fatal; storage_state already saved

            # ── Close browser (persistent-context has no separate _brow) ──────────
            try:
                await _bctx.close()
            except Exception:
                pass
            if _brow is not None:
                try:
                    await _brow.close()
                except Exception:
                    pass

            # P0-5: Release advisory lock
            if _lock_acquired and _lock_conn is not None and _lock_key is not None:
                try:
                    await _lock_conn.execute(f"SELECT pg_advisory_unlock({_lock_key})")
                    await _lock_conn.close()
                    logger.info(f"dag_run.lock: released advisory lock for '{_email}'")
                except Exception:
                    pass

            # ── Auto-trace: POST executed steps as memory nodes to XIOSYNC ──────
            if _trace_requested and trace_nodes and all_ok:
                try:
                    _org_id = context.get("organization_id", "00000000-0000-7000-8000-000000000000")
                    async with _hx.AsyncClient(timeout=20) as _cl:
                        _tr = await _cl.post(
                            f"{_XIOSYNC}/api/v1/xioflow/events/trace-nodes-internal",
                            headers=_HDRS,
                            json={
                                "org_id": _org_id,
                                "domain": dag_domain,
                                "nodes": trace_nodes,
                                "run_id": run_id,
                            },
                        )
                    logger.info(
                        f"dag_run.trace: deployed {_tr.json().get('deployed', 0)} "
                        f"memory nodes via auto-trace (HTTP {_tr.status_code})"
                    )
                except Exception as _tre:
                    logger.warning(f"dag_run.trace: deploy failed ({_tre})")

        # 3. Report results back to XIOSYNC
        try:
            async with _hx.AsyncClient(timeout=15) as _cl:
                await _cl.post(
                    f"{_XIOSYNC}/api/v1/xioflow/events/runs-internal/{run_id}/complete",
                    headers=_HDRS,
                    json={"success": all_ok, "task_id": task_id, "result": {"steps": results}},
                )
        except BaseException as _cr:
            logger.warning(
                f"dag_run: complete-report failed ({type(_cr).__name__}: {_cr}) — result still logged"
            )
        logger.info(f"dag_run.done: run={run_id} ok={all_ok} steps={len(results)}")

    except BaseException as _ex:
        logger.error(f"dag_run.fatal: run={run_id} {_ex}")
        try:
            import httpx as _hx2

            async with _hx2.AsyncClient(timeout=10) as _cl:
                await _cl.post(
                    f"{_XIOSYNC}/api/v1/xioflow/events/runs-internal/{run_id}/complete",
                    headers=_HDRS,
                    json={"success": False, "task_id": task_id, "error": str(_ex)},
                )
        except Exception:
            pass
        if isinstance(_ex, __import__("asyncio").CancelledError):
            raise
