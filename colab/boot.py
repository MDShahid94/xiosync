#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════════════════════════
# XIOSYNC Colab Worker Boot Script  (colab/boot.py)
# ════════════════════════════════════════════════════════════════════════════════
# Fetched and exec()'d by start.ipynb Cell 2 on every Colab runtime startup.
# All boot logic lives here — never edit the notebook itself.
#
# Globals available from notebook Cell 1:
#   CONFIG  — dict (can also be loaded from /tmp/xio_config.json)
#   __name__ == '__boot__'
#
# Phases:
#   0  System packages (chromium deps, unzip, curl)
#   1  Tailscale install + connect (persisted state from GDrive/R2)
#   2  Python deps (patchright, boto3, fastapi, uvicorn, httpx)
#   3  patchright Chromium binary
#   4  XIOSYNC self-enroll + heartbeat thread
#   5  xiorun_agent start (FastAPI :9300)
#   6  Keepalive + watchdog
# ════════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

# ── Config resolution — three sources in priority order ───────────────────────
#
# 1. Bootstrap token (XIOSYNC-native, preferred):
#    Colab notebook passes BOOTSTRAP_TOKEN + XIOSYNC_BASE.
#    boot.py GETs /api/v1/workers/bootstrap/{token} → full CONFIG dict.
#
# 2. Legacy CONFIG dict injected by exec() caller (for backward compat):
#    globals().get("CONFIG") — set by notebook Cell 2 if using old approach.
#
# 3. /tmp/xio_config.json — written by notebook before exec()
#
# Always prefer the bootstrap endpoint — it's the XIOSYNC-native way.
# Secrets never appear in notebook code or git history.

_BOOTSTRAP_TOKEN = globals().get("BOOTSTRAP_TOKEN") or \
                   os.environ.get("XIOSYNC_BOOTSTRAP_TOKEN", "")
_XIOSYNC_BASE_HINT = globals().get("XIOSYNC_BASE") or \
                     os.environ.get("XIOSYNC_PUBLIC_URL", "")

C: dict = {}

if _BOOTSTRAP_TOKEN and _XIOSYNC_BASE_HINT:
    # ── Path 1: Fetch config from XIOSYNC ────────────────────────────────────
    print(f"Fetching worker config from XIOSYNC ({_XIOSYNC_BASE_HINT})…", flush=True)
    import urllib.request as _urq
    _MAX_ATTEMPTS = 3
    for _attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            _resp = _urq.urlopen(
                f"{_XIOSYNC_BASE_HINT}/api/v1/workers/bootstrap/{_BOOTSTRAP_TOKEN}",
                timeout=15,
            )
            _payload = json.loads(_resp.read())
            C = _payload.get("config", {})
            _node_from_server = _payload.get("node_name", "")
            if _node_from_server:
                C.setdefault("node_name", _node_from_server)
            print(f"✅ Config fetched from XIOSYNC (node={C.get('node_name')})", flush=True)
            break
        except Exception as _be:
            print(f"⚠️  Config fetch attempt {_attempt}/{_MAX_ATTEMPTS} failed: {_be}", flush=True)
            if _attempt < _MAX_ATTEMPTS:
                time.sleep(3)
            else:
                print("❌ Cannot fetch worker config — check XIOSYNC_BASE and token", flush=True)
else:
    # ── Path 2/3: Legacy / local config ──────────────────────────────────────
    C = globals().get("CONFIG") or {}
    if not C:
        try:
            with open("/tmp/xio_config.json") as _f:
                C = json.load(_f)
        except Exception:
            pass
    if C:
        print("ℹ️  Using locally-provided CONFIG (consider using bootstrap token instead)", flush=True)
    else:
        print("⚠️  No config available — continuing with defaults only", flush=True)

NODE_NAME        = C.get("node_name",          "xiogrid--default--worker-001")
XIOSYNC_BASE     = C.get("xiosync_url",         "")
XIOSYNC_TOKEN    = C.get("xiosync_token",        "")
WORKER_SECRET    = C.get("xiosync_worker_secret","")
INTERNAL_SECRET  = C.get("xiosync_internal_secret", "")
GH_PAT           = C.get("gh_pat",               "")
TS_AUTH_KEY      = C.get("tailscale_auth_key",    "")
TS_EXIT_NODE_IP  = C.get("default_exit",          "")
XIORUN_PORT      = int(C.get("xiorun_agent_port", 9300))
R2_ENDPOINT      = C.get("r2_endpoint",           "")
R2_BUCKET        = C.get("r2_bucket",             "xio-profiles")
R2_ACCESS_KEY    = C.get("r2_access_key",          "")
R2_SECRET_KEY    = C.get("r2_secret_key",          "")
LOCAL_ROOT       = C.get("local_root",             "/content/xiosync-worker")

# Org/project/role context — delivered by XIOSYNC bootstrap
ORG_SLUG     = C.get("org_slug",     "xiogrid")
PROJECT_SLUG = C.get("project_slug", "default")
ROLE         = C.get("role",         "worker")

# Stable identity: no counter suffix — used for TS device name, Drive keys, vault keys.
# Delivered by XIOSYNC directly; fallback strips -NNN suffix for old tokens.
import re as _re  # noqa: PLC0415
_NODE_IDENTITY = C.get("node_identity") or _re.sub(r"-\d{3,}$", "", NODE_NAME)

# Drive FUSE mount config (delivered by XIOSYNC bootstrap)
DRIVE_FOLDER_ID     = C.get("drive_folder_id",     "19k79lkPzg1gBM7IhIhE-35rfiAyVfCsK")
DRIVE_SHORTCUT_NAME = C.get("drive_shortcut_name", "XIOSYNC-Shared")
DRIVE_FS_ROOT       = C.get("drive_fs_root",       f"/content/drive/MyDrive/{DRIVE_SHORTCUT_NAME}")

# Keep the original bootstrap URL (e.g. Cloudflare tunnel) for fetching public
# artifacts before Tailscale connects. XIOSYNC_BASE may be a Tailscale IP that
# is only reachable AFTER Phase 1 completes.
BOOTSTRAP_XIOSYNC_BASE = _XIOSYNC_BASE_HINT or XIOSYNC_BASE

# ASSET_BASE: use bootstrap URL for public static fetches (boot.py, agent);
# switches to XIOSYNC_BASE (Tailscale) once TS is confirmed connected.
ASSET_BASE = BOOTSTRAP_XIOSYNC_BASE or XIOSYNC_BASE

os.makedirs(LOCAL_ROOT, exist_ok=True)
os.makedirs("/tmp/xiorun_profiles", exist_ok=True)

# Module-level reference to Drive FS helper (initialised in Phase 0.5)
_XIO_FS = None   # type: XIODriveFS | None


def _p(msg: str) -> None:
    print(msg, flush=True)


def _run(cmd: str, *, timeout: int = 120, silent: bool = False) -> int:
    kwargs: dict = {"shell": True, "timeout": timeout}
    if silent:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    return subprocess.run(cmd, **kwargs).returncode


# ════════════════════════════════════════════════════════════════════════════════
# PHASE 0: System packages
# ════════════════════════════════════════════════════════════════════════════════
_p("\n" + "═" * 60)
_p("  Phase 0: System packages")
_p("═" * 60)

_APT_DEPS = [
    "libnss3", "libatk1.0-0", "libatk-bridge2.0-0", "libx11-xcb1", "libxcomposite1",
    "libxdamage1", "libxrandr2", "libgbm1", "libcups2", "libxkbcommon0",
    "xvfb", "unzip", "curl", "openssh-server",
]
# libasound2 was renamed to libasound2t64 in Ubuntu 24.04 (Colab updated runtime)
_LIBASOUND = "libasound2t64" if _run("apt-cache show libasound2t64 > /dev/null 2>&1", silent=True) == 0 else "libasound2"
_APT_DEPS.append(_LIBASOUND)
_missing = [p for p in _APT_DEPS if _run(f"dpkg -s {p}", silent=True) != 0]
if _missing:
    _p(f"  Installing {len(_missing)} system deps…")
    _run(f"DEBIAN_FRONTEND=noninteractive apt-get install -y -q --fix-missing {' '.join(_missing)} > /dev/null 2>&1",
         timeout=300)
else:
    _p("  ✅ All system deps already installed")

# ── SSH setup (pubkey-only, root login via Tailscale) ─────────────────────────
_MAC_PUBKEY = C.get("ssh_authorized_key", "")
os.makedirs("/root/.ssh", mode=0o700, exist_ok=True)
_AK = "/root/.ssh/authorized_keys"
_ak_existing = open(_AK).read() if os.path.exists(_AK) else ""
if _MAC_PUBKEY and _MAC_PUBKEY not in _ak_existing:
    with open(_AK, "a") as _f:
        _f.write(_MAC_PUBKEY.strip() + "\n")
elif not os.path.exists(_AK):
    open(_AK, "w").close()   # create empty file so chmod never fails
os.chmod(_AK, 0o600)

_sshd_cfg = "/etc/ssh/sshd_config"
_sshd_txt = open(_sshd_cfg).read() if os.path.exists(_sshd_cfg) else ""
if "PermitRootLogin yes" not in _sshd_txt:
    with open(_sshd_cfg, "a") as _sc:
        _sc.write("\nPermitRootLogin yes\nPasswordAuthentication no\nPubkeyAuthentication yes\n")
if os.system("pgrep -x sshd > /dev/null") != 0:
    os.system("service ssh start > /dev/null 2>&1 || /usr/sbin/sshd")

# Generate this node's SSH identity (used for master→worker SSH)
_NODE_SSH_ID = "/root/.ssh/id_ed25519"
if not os.path.exists(_NODE_SSH_ID):
    os.system(f"ssh-keygen -t ed25519 -f {_NODE_SSH_ID} -N '' > /dev/null 2>&1")

# Start Xvfb (virtual display)
if os.system("pgrep Xvfb > /dev/null") != 0:
    _run("Xvfb :99 -screen 0 1920x1080x24 &", silent=True)
    os.environ["DISPLAY"] = ":99"
    _p("  ✅ Xvfb started on :99")


# ════════════════════════════════════════════════════════════════════════════════
# PHASE 0.5: Google Drive FUSE mount + XIODriveFS initialisation
# ════════════════════════════════════════════════════════════════════════════════
_p("\n" + "═" * 60)
_p("  Phase 0.5: Drive FUSE mount")
_p("═" * 60)

try:
    # 1. Fetch xio_drive_fs.py from XIOSYNC (served as a public static asset)
    import urllib.request as _urq  # noqa: PLC0415
    _fs_module_path = "/tmp/xio_drive_fs.py"
    _fs_url = f"{ASSET_BASE}/api/v1/workers/xio-drive-fs.py"
    try:
        _fs_src = _urq.urlopen(_fs_url, timeout=10).read().decode()
        with open(_fs_module_path, "w") as _f:
            _f.write(_fs_src)
        _p(f"  ✅ Fetched xio_drive_fs.py from XIOSYNC")
    except Exception as _fe:
        _p(f"  ⚠️  xio_drive_fs.py fetch failed: {_fe} — Drive FUSE skipped")
        raise  # jump to outer except

    # 2. Import the module
    import importlib.util as _ilu  # noqa: PLC0415
    _spec = _ilu.spec_from_file_location("xio_drive_fs", _fs_module_path)
    _xdf_mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_xdf_mod)

    # 3. Mount Drive and ensure the XIOSYNC-Shared shortcut exists
    _drive_root = _xdf_mod.mount_drive_and_ensure_shortcut(
        folder_id=DRIVE_FOLDER_ID,
        shortcut_name=DRIVE_SHORTCUT_NAME,
    )

    if _drive_root:
        # 4. Initialise XIODriveFS instance (used by Phase 1+ for all blob I/O)
        _XIO_FS = _xdf_mod.XIODriveFS(
            xiosync_base=ASSET_BASE,
            worker_secret=WORKER_SECRET,
            node_name=_NODE_IDENTITY,
            drive_fs_root=_drive_root,
        )
        _p(f"  ✅ XIODriveFS ready → {_drive_root}")
    else:
        _p("  ⚠️  Drive mount returned None — falling back to XIOSYNC API for blob I/O")

except Exception as _drive_ex:
    _p(f"  ℹ️  Drive FUSE setup skipped ({type(_drive_ex).__name__}) — non-fatal")


# ════════════════════════════════════════════════════════════════════════════════
# PHASE 1: Tailscale
# ════════════════════════════════════════════════════════════════════════════════
_p("\n" + "═" * 60)
_p("  Phase 1: Tailscale")
_p("═" * 60)

_my_ts_ip: str = ""
_ts_online: bool = False

# _NODE_IDENTITY is derived at config load (top of file): "xiogrid--default--master"
_TS_STATE_DIR  = "/var/lib/tailscale"
_TS_STATE_FILE = f"{_TS_STATE_DIR}/tailscaled.state"

# 1. Install Tailscale binary if missing
if not shutil.which("tailscale"):
    _p("  Installing Tailscale…")
    _run("curl -fsSL https://tailscale.com/install.sh | sh > /dev/null 2>&1", timeout=120)

if not TS_AUTH_KEY:
    _p("  ℹ️  No tailscale_auth_key in config — skipping Tailscale")
else:
    # 2. Kill any stale tailscaled daemon (must stop before we touch state file)
    subprocess.run("pkill -9 tailscaled 2>/dev/null; sleep 1",
                   shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.makedirs(_TS_STATE_DIR, mode=0o700, exist_ok=True)

    # 2b. Restore persisted Tailscale state from Drive FUSE BEFORE starting tailscaled.
    #
    #     Auth key kHWfpLrApB11CNTRL (XIOSYNC_AUTH) is REUSABLE, expires Dec 11 2026.
    #     With a reusable key + restored state file, tailscaled reconnects the SAME
    #     node identity (same Tailscale IP + machine name) — no new machine is created.
    #     Without this restore the state file is wiped → fresh auth → new machine every boot,
    #     which was causing the proliferation of dead nodes visible in the Tailscale dashboard.
    #
    #     Drive FUSE (_XIO_FS) is guaranteed initialised in Phase 0.5 before this phase.
    _ts_key = f"ts_states/TS_{_NODE_IDENTITY}.state"
    _ts_state_restored = False
    try:
        _state_bytes_restore: bytes | None = None
        if _XIO_FS is not None:
            _state_bytes_restore = _XIO_FS.get(_ts_key)
            _restore_source = "Drive FUSE"
        else:
            # Fallback: GET from XIOSYNC ts-state API (for nodes without Drive FUSE)
            import urllib.request as _urq_ts  # noqa: PLC0415
            try:
                _ts_get_req = _urq_ts.Request(
                    f"{ASSET_BASE}/api/v1/workers/ts-state/{_NODE_IDENTITY}",
                    headers={"X-Worker-Secret": WORKER_SECRET},
                    method="GET",
                )
                _raw = _urq_ts.urlopen(_ts_get_req, timeout=10).read()
                _state_bytes_restore = _raw if _raw else None
                _restore_source = "XIOSYNC API"
            except Exception:
                _state_bytes_restore = None
                _restore_source = "none"

        if _state_bytes_restore:
            with open(_TS_STATE_FILE, "wb") as _sf:
                _sf.write(_state_bytes_restore)
            _ts_state_restored = True
            _p(f"  ✅ TS state restored from {_restore_source} "
               f"({len(_state_bytes_restore)} bytes) — will reconnect existing node")
        else:
            # No saved state: wipe any stale leftover and do a fresh registration
            try:
                os.remove(_TS_STATE_FILE)
            except Exception:
                pass
            _p("  ℹ️  No saved TS state found — fresh Tailscale login (new node will be created)")
    except Exception as _ts_restore_ex:
        try:
            os.remove(_TS_STATE_FILE)
        except Exception:
            pass
        _p(f"  ⚠️  TS state restore failed ({_ts_restore_ex}) — falling back to fresh auth")

    # 3. Start tailscaled — modprobe tun first, fallback to userspace-networking
    _tun_ok = os.system("modprobe tun > /dev/null 2>&1") == 0
    _tsd_bin = (shutil.which("tailscaled")
                or next((p for p in ["/usr/sbin/tailscaled", "/usr/local/sbin/tailscaled",
                                     "/usr/bin/tailscaled"] if os.path.exists(p)), None))
    if not _tsd_bin:
        _p("  ❌ tailscaled binary not found — skipping Tailscale")
    else:
        if _tun_ok:
            os.system(f"nohup {_tsd_bin} --state={_TS_STATE_FILE} > /tmp/tailscaled.log 2>&1 &")
        else:
            os.system(f"nohup {_tsd_bin} --state={_TS_STATE_FILE}"
                      f" --tun=userspace-networking --socks5-server=localhost:1055"
                      f" > /tmp/tailscaled.log 2>&1 &")
        time.sleep(3)

        # 4. Authenticate — omit --reset when state was restored so the existing
        #    node identity is reused (same Tailscale IP, same machine slot).
        #    --reset is only added on first-ever boot (no saved state available).
        _ts_bin = shutil.which("tailscale") or "/usr/bin/tailscale"
        _p(f"  Connecting as '{_NODE_IDENTITY}' "
           f"({'reconnecting existing node' if _ts_state_restored else 'fresh registration'})…")
        _ts_up_args = [
            _ts_bin, "up",
            f"--authkey={TS_AUTH_KEY}",
            f"--hostname={_NODE_IDENTITY}",
            "--accept-routes", "--ssh",
        ]
        if not _ts_state_restored:
            # First-ever boot for this node identity: --reset ensures a clean slate
            _ts_up_args.append("--reset")
        _ts_up = subprocess.run(
            _ts_up_args,
            capture_output=True, text=True, timeout=90,
        )
        if _ts_up.returncode == 0:
            time.sleep(3)
            _ip_out = subprocess.run([_ts_bin, "ip", "-4"],
                                      capture_output=True, text=True, timeout=5)
            _my_ts_ip = _ip_out.stdout.strip()
            _ts_online = bool(_my_ts_ip)
            _p(f"  ✅ Tailscale connected: {_my_ts_ip}" if _ts_online
               else "  ⚠️  tailscale up OK but no IP returned")
        else:
            _ts_err = (_ts_up.stdout + _ts_up.stderr).strip()
            _p(f"  ⚠️  tailscale up failed (rc={_ts_up.returncode}): {_ts_err[:200]}")
            try:
                _daemon_out = open("/tmp/tailscaled.log").read().strip()
                if _daemon_out:
                    _p(f"  tailscaled: {_daemon_out[-200:]}")
            except Exception:
                pass

        # 5. SSH pubkey: store on Drive FUSE (primary) or XIOSYNC API (fallback)
        try:
            import urllib.request as _urq  # noqa: PLC0415
            _node_pubkey = open(f"{_NODE_SSH_ID}.pub").read().strip()
            if _XIO_FS is not None:
                _XIO_FS.put(f"ssh_pubkeys/{_NODE_IDENTITY}.pub",
                            _node_pubkey.encode(), skip_if_same=True)
            else:
                _pk_req = _urq.Request(
                    f"{ASSET_BASE}/api/v1/workers/ssh-pubkey/{_NODE_IDENTITY}",
                    data=_node_pubkey.encode(),
                    headers={"Content-Type": "text/plain", "X-Worker-Secret": WORKER_SECRET},
                    method="PUT",
                )
                _urq.urlopen(_pk_req, timeout=10)
        except Exception:
            pass  # non-fatal

        # 6. Save TS state: Drive FUSE (primary, zero API quota) → XIOSYNC API (fallback)
        if _ts_online and os.path.exists(_TS_STATE_FILE):
            try:
                import urllib.request as _urq  # noqa: PLC0415
                _state_bytes = open(_TS_STATE_FILE, "rb").read()

                if _XIO_FS is not None:
                    # Primary: write directly to Drive FUSE mount (no API quota)
                    _written = _XIO_FS.put(_ts_key, _state_bytes, skip_if_same=True)
                    _p("  ✅ TS state saved to Drive FUSE" + (" (unchanged)" if not _written else ""))
                else:
                    # Fallback: PUT to XIOSYNC server which caches locally
                    _save_req = _urq.Request(
                        f"{ASSET_BASE}/api/v1/workers/ts-state/{_NODE_IDENTITY}",
                        data=_state_bytes,
                        headers={"Content-Type": "application/octet-stream",
                                 "X-Worker-Secret": WORKER_SECRET},
                        method="PUT",
                    )
                    _urq.urlopen(_save_req, timeout=10)
                    _p("  ✅ TS state saved via XIOSYNC API (Drive FUSE unavailable)")
            except Exception as _se:
                _p(f"  ℹ️  TS state save skipped ({_se.__class__.__name__}: {_se})")


# ════════════════════════════════════════════════════════════════════════════════
# PHASE 2: Python dependencies
# ════════════════════════════════════════════════════════════════════════════════
_p("\n" + "═" * 60)
_p("  Phase 2: Python dependencies")
_p("═" * 60)

_PYTHON_DEPS = [
    # ── Primary browser engine: undetected-chromedriver (UC) ──────────────────
    # UC binary-patches Chrome at launch to remove webdriver artifacts at native
    # level — proven to pass Google bot-detection where patchright/CDP fails.
    # User directive: UC is the ONLY active engine in Colab for all browser activity.
    "undetected-chromedriver>=3.5.5",
    "selenium>=4.18",
    "camoufox>=0.4.0",        # Firefox C++-level spoofing (fallback engine)
    "Pillow>=10.0",            # JPEG screenshots via uc_snap()
    # ── Agent / API framework ─────────────────────────────────────────────────
    "fastapi[standard]>=0.111",
    "uvicorn[standard]>=0.29",
    "httpx>=0.27",
    "boto3>=1.34",
    "pydantic>=2.7",
    "structlog>=24.1",          # used by ai_healer, intent_indexer, and other subsystems
    "google-generativeai>=0.8", # AIHealer Tier-10 Gemini LLM provider
    # ── Patchright kept as secondary (non-auth DAG navigation fallback) ────────
    "patchright==1.62.3",
]

# ── Drive cache fast-path: extract pre-bundled wheels from Drive FUSE ──────────
_PY_CACHE_KEY  = "cache/python/stealth-pkgs-v1.tar.gz"
_PY_CACHE_PATH = None
if _XIO_FS is not None:
    _py_drive_path = os.path.join(_drive_root, _PY_CACHE_KEY)
    if os.path.exists(_py_drive_path):
        _PY_CACHE_PATH = _py_drive_path
    else:
        # Try XIODriveFS.get() for non-FUSE path
        try:
            _py_data = _XIO_FS.get(_PY_CACHE_KEY)
            if _py_data:
                _PY_CACHE_PATH = f"/tmp/{os.path.basename(_PY_CACHE_KEY)}"
                with open(_PY_CACHE_PATH, "wb") as _f:
                    _f.write(_py_data)
        except Exception:
            pass

if _PY_CACHE_PATH and os.path.exists(_PY_CACHE_PATH):
    _p(f"  ⚡ Drive cache hit — extracting Python wheels from Drive…")
    import tarfile as _tf
    _wheel_dir = "/tmp/xio-pkgs"
    os.makedirs(_wheel_dir, exist_ok=True)
    with _tf.open(_PY_CACHE_PATH, "r:gz") as _tar:
        _tar.extractall(_wheel_dir)
    _deps_str = " ".join(f'"{d}"' for d in _PYTHON_DEPS)
    _p(f"  Installing {len(_PYTHON_DEPS)} packages from local wheels…")
    rc = _run(
        f"\"{sys.executable}\" -m pip install -q --no-index --find-links {_wheel_dir} {_deps_str} 2>&1 | tail -2",
        timeout=120,
    )
    if rc != 0:
        _p("  ⚠️  Offline install failed — falling back to PyPI…")
        rc = _run(f"\"{sys.executable}\" -m pip install -q {_deps_str} 2>&1 | tail -4", timeout=300)
else:
    _p(f"  Installing {len(_PYTHON_DEPS)} Python packages from PyPI…")
    _deps_str = " ".join(f'"{d}"' for d in _PYTHON_DEPS)
    rc = _run(f"\"{sys.executable}\" -m pip install -q {_deps_str} 2>&1 | tail -4", timeout=300)

_p(f"  {'✅' if rc == 0 else '⚠️ '} pip install {'OK' if rc == 0 else 'had warnings (check above)'}")

# Guard: verify patchright is importable (wheel cache may have missed it due to
# Python version mismatch). Install directly from PyPI if needed.
try:
    import importlib.util as _ilu
    if _ilu.find_spec("patchright") is None:
        raise ImportError("not found")
except (ImportError, ValueError):
    _p("  ⚠️  patchright not in wheel cache — installing from PyPI…")
    _pr_rc = _run(f'"{sys.executable}" -m pip install -q "patchright==1.62.3" 2>&1 | tail -2', timeout=180)
    _p(f"  {'✅' if _pr_rc == 0 else '❌'} patchright PyPI install {'OK' if _pr_rc == 0 else 'FAILED'}")

# ════════════════════════════════════════════════════════════════════════════════
# PHASE 3: patchright Chromium binary
# ════════════════════════════════════════════════════════════════════════════════
_p("\n" + "═" * 60)
_p("  Phase 3: patchright Chromium binary")
_p("═" * 60)

# ── Drive cache fast-path: restore Chromium binaries from Drive FUSE ───────────
_PW_CACHE_KEY  = "cache/playwright/ms-playwright-v1.tar.gz"
_PW_CACHE_PATH = None
if _XIO_FS is not None:
    _pw_drive_path = os.path.join(_drive_root, _PW_CACHE_KEY)
    if os.path.exists(_pw_drive_path):
        _PW_CACHE_PATH = _pw_drive_path
    else:
        try:
            _pw_data = _XIO_FS.get(_PW_CACHE_KEY)
            if _pw_data:
                _PW_CACHE_PATH = f"/tmp/{os.path.basename(_PW_CACHE_KEY)}"
                with open(_PW_CACHE_PATH, "wb") as _f:
                    _f.write(_pw_data)
        except Exception:
            pass

# Check if chromium is already installed
_chrome_check = subprocess.run(
    [sys.executable, "-c",
     "import patchright; from patchright.sync_api import sync_playwright; "
     "p=sync_playwright().start(); "
     "print(p.chromium.executable_path); p.stop()"],
    capture_output=True, text=True, timeout=15,
)
_chrome_path = _chrome_check.stdout.strip()
_chrome_ok = bool(_chrome_path) and os.path.exists(_chrome_path)

if _chrome_ok:
    _p(f"  ✅ patchright Chromium already at: {_chrome_path}")
elif _PW_CACHE_PATH and os.path.exists(_PW_CACHE_PATH):
    _p("  ⚡ Drive cache hit — restoring Chromium from Drive…")
    import tarfile as _tf2
    _pw_dest = os.path.expanduser("~/.cache/ms-playwright")
    os.makedirs(_pw_dest, exist_ok=True)
    with _tf2.open(_PW_CACHE_PATH, "r:gz") as _tar2:
        _tar2.extractall(_pw_dest)
    _p("  ✅ patchright Chromium restored from Drive cache")
    # Install system deps for the restored binary (does NOT re-download the browser)
    _p("  Installing system deps for Chromium…")
    _run(
        f"{sys.executable} -m patchright install chromium --with-deps > /tmp/patchright_deps.log 2>&1 | tail -2",
        timeout=120,
    )
    _p("  ✅ patchright system deps ready")
else:
    _p("  Installing patchright Chromium (≈177 MB, one-time)…")
    rc = _run(
        f"{sys.executable} -m patchright install chromium --with-deps > /tmp/patchright_install.log 2>&1",
        timeout=300,
    )
    if rc == 0:
        _p("  ✅ patchright Chromium installed")
    else:
        _p("  ⚠️  patchright Chromium install failed — see /tmp/patchright_install.log")


# ════════════════════════════════════════════════════════════════════════════════
# PHASE 4: XIOSYNC self-enroll + heartbeat
# ════════════════════════════════════════════════════════════════════════════════
_p("\n" + "═" * 60)
_p("  Phase 4: XIOSYNC self-enroll")
_p("═" * 60)

_ENROLLMENT_ID: str | None = None


def _xiosync_post(path: str, data: dict, *, timeout: int = 15) -> dict:
    import urllib.request as _urq  # noqa
    # Use ASSET_BASE (bootstrap/tunnel URL) so this works before Tailscale connects.
    # After Tailscale, heartbeats switch to XIOSYNC_BASE directly.
    _base = ASSET_BASE or XIOSYNC_BASE
    req = _urq.Request(
        f"{_base}{path}",
        data=json.dumps(data).encode(),
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {XIOSYNC_TOKEN}",
        },
        method="POST",
    )
    resp = _urq.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


if XIOSYNC_BASE and WORKER_SECRET:
    try:
        _enr = _xiosync_post(
            "/api/v1/workers/self-enroll",
            {
                "worker_org_secret": WORKER_SECRET,
                "runtime_type":      "colab",
                "tailscale_ip":      _my_ts_ip,
                "reported_caps":     [
                    "browser.run",
                    "xiorun.agent",
                    "xioflow.execute",
                ],
                "software_version": "xiosync-boot-v1.0",
            },
        )
        _ENROLLMENT_ID = _enr.get("enrollment_id")
        _p(f"  ✅ Self-enrolled: enrollment_id={_ENROLLMENT_ID} "
           f"state={_enr.get('enrollment_state')}")
    except Exception as _err:
        _p(f"  ⚠️  Self-enroll failed (non-fatal): {_err}")
else:
    _p("  ℹ️  Skipping self-enroll — xiosync_url or xiosync_worker_secret not set")


def _heartbeat_loop() -> None:
    """Send heartbeat to XIOSYNC every 30 seconds."""
    if not (_ENROLLMENT_ID and XIOSYNC_BASE):
        return
    import urllib.request as _urq  # noqa
    while True:
        try:
            req = _urq.Request(
                f"{XIOSYNC_BASE}/api/v1/workers/{_ENROLLMENT_ID}/heartbeat",
                data=json.dumps({
                    "tailscale_ip": _my_ts_ip,
                    "reported_caps": ["browser.run", "xiorun.agent", "xioflow.execute"],
                }).encode(),
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": f"Bearer {XIOSYNC_TOKEN}",
                },
                method="POST",
            )
            _urq.urlopen(req, timeout=10)
        except Exception:
            pass
        time.sleep(30)


_hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True, name="xiosync-heartbeat")
_hb_thread.start()


# ════════════════════════════════════════════════════════════════════════════════
# PHASE 5: xiorun_agent
# ════════════════════════════════════════════════════════════════════════════════
_p("\n" + "═" * 60)
_p("  Phase 5: xiorun_agent (FastAPI :9300)")
_p("═" * 60)

# Kill any stale agent from a previous run
_run("pkill -f 'xiorun_agent.py' > /dev/null 2>&1", silent=True)
time.sleep(1)

# Fetch xiorun_agent.py from XIOSYNC (served at /api/v1/workers/xiorun-agent.py)
# Use ASSET_BASE (bootstrap/Cloudflare URL) — reachable before Tailscale connects.
# Falls back to local copy if unreachable.
_AGENT_URL = f"{ASSET_BASE}/api/v1/workers/xiorun-agent.py"
_AGENT_PATH = "/tmp/xiorun_agent.py"

_fetched = False
try:
    import urllib.request as _urq  # noqa
    _urq.urlretrieve(_AGENT_URL, _AGENT_PATH)
    _fetched = True
    _p(f"  ✅ Fetched xiorun_agent.py from XIOSYNC")
except Exception as _fe:
    _p(f"  ⚠️  Could not fetch xiorun_agent.py from XIOSYNC ({_fe}) — checking local copy…")
    # Fallback: local copy bundled in the Colab environment
    _local_agent = os.path.join(LOCAL_ROOT, "xiorun_agent.py")
    if os.path.exists(_local_agent):
        shutil.copy(_local_agent, _AGENT_PATH)
        _fetched = True
        _p("  ✅ Using local copy of xiorun_agent.py")
    else:
        _p("  ❌ xiorun_agent.py not available — browser sessions will NOT work")

_agent_proc: subprocess.Popen | None = None

if _fetched:
    _agent_env = {
        **os.environ,
        "XIORUN_AGENT_PORT":        str(XIORUN_PORT),
        "XIORUN_R2_ENDPOINT":       R2_ENDPOINT,
        "XIORUN_R2_BUCKET":         R2_BUCKET,
        "XIORUN_R2_ACCESS_KEY":     R2_ACCESS_KEY,
        "XIORUN_R2_SECRET_KEY":     R2_SECRET_KEY,
        "XIORUN_XIOSYNC_BASE":      XIOSYNC_BASE,
        "XIORUN_XIOSYNC_TOKEN":     INTERNAL_SECRET,  # Colab → XIOSYNC internal secret
        "XIO_NODE_NAME":            NODE_NAME,         # legacy key
        # ── profile_store / XIODriveFS init ──────────────────────────────────
        "XIOSYNC_BASE":             XIOSYNC_BASE,
        "WORKER_SECRET":            WORKER_SECRET,
        "NODE_NAME":                NODE_NAME,
        "XIO_DRIVE_ROOT":           _drive_root or "/content/drive/MyDrive/XIOSYNC-Shared",
        # patchright Chromium location (restored from Drive cache by Phase 3)
        "PLAYWRIGHT_BROWSERS_PATH": os.path.expanduser("~/.cache/ms-playwright/ms-playwright"),
        # ─────────────────────────────────────────────────────────────────────
        "DISPLAY":                  ":99",
    }
    _agent_log = open("/tmp/xiorun_agent.log", "w")
    _agent_proc = subprocess.Popen(
        [sys.executable, _AGENT_PATH],
        env=_agent_env,
        stdout=_agent_log,
        stderr=_agent_log,
    )
    atexit.register(lambda: _agent_proc.terminate() if _agent_proc else None)

    # Wait for agent to be ready (max 10s)
    _ready = False
    for _ in range(20):
        time.sleep(0.5)
        try:
            import urllib.request as _urq  # noqa
            _health = json.loads(
                _urq.urlopen(f"http://127.0.0.1:{XIORUN_PORT}/health", timeout=2).read()
            )
            if _health.get("ok"):
                _ready = True
                break
        except Exception:
            pass

    if _ready:
        _p(f"  ✅ xiorun_agent ready on :{XIORUN_PORT} "
           f"(PID {_agent_proc.pid}) — Tailscale: {_my_ts_ip}:{XIORUN_PORT}")
    else:
        _p(f"  ⚠️  xiorun_agent started (PID {_agent_proc.pid}) but health check timed out "
           f"— see /tmp/xiorun_agent.log")


# ════════════════════════════════════════════════════════════════════════════════
# PHASE 6: Keepalive + watchdog
# ════════════════════════════════════════════════════════════════════════════════
_p("\n" + "═" * 60)
_p("  Phase 6: Watchdog")
_p("═" * 60)


import random as _rand  # noqa: PLC0415
_BOOT_GEN = _rand.randint(100000, 999999)
with open("/tmp/xio_boot_gen", "w") as _bg:
    _bg.write(str(_BOOT_GEN))


def _watchdog_loop() -> None:
    """Restart xiorun_agent if it dies unexpectedly.

    Exits silently if a newer boot generation has started (stale watchdog guard).
    """
    global _agent_proc
    if not _fetched:
        return
    while True:
        time.sleep(15)
        # Stop if a newer boot has taken over
        try:
            with open("/tmp/xio_boot_gen") as _bgf:
                if int(_bgf.read().strip()) != _BOOT_GEN:
                    return  # newer boot started — let its watchdog take over
        except Exception:
            pass
        if _agent_proc and _agent_proc.poll() is not None:
            _p(f"  ⚠️  xiorun_agent died (exit={_agent_proc.returncode}) — restarting…")
            try:
                _agent_log2 = open("/tmp/xiorun_agent.log", "a")
                _agent_proc = subprocess.Popen(
                    [sys.executable, _AGENT_PATH],
                    env=_agent_env,
                    stdout=_agent_log2,
                    stderr=_agent_log2,
                )
                _p(f"  ✅ xiorun_agent restarted (PID {_agent_proc.pid})")
            except Exception as _we:
                _p(f"  ❌ watchdog restart failed: {_we}")


_wd_thread = threading.Thread(target=_watchdog_loop, daemon=True, name="xiorun-watchdog")
_wd_thread.start()

_p("\n" + "═" * 60)
_p(f"  Boot complete ✅")
_p(f"  Node:          {NODE_NAME}")
_p(f"  Tailscale IP:  {_my_ts_ip or 'not connected'}")
_p(f"  XIOSYNC:       {XIOSYNC_BASE or 'not configured'}")
_p(f"  xiorun_agent:  :{XIORUN_PORT} (PID {_agent_proc.pid if _agent_proc else 'N/A'})")
_p(f"  Enrollment ID: {_ENROLLMENT_ID or 'not enrolled'}")
_p("═" * 60 + "\n")
