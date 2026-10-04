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
#   2  Python deps (UC, boto3, fastapi, uvicorn, httpx, structlog, Pillow)
#   3  Chrome / UC readiness check (UC auto-downloads on first use)
#   4  XIOSYNC self-enroll + heartbeat thread
#   5  xiorun_agent start (FastAPI :9300)
#   6  Keepalive + watchdog
# ════════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
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


def _run(cmd: str, *, timeout: int = 120, silent: bool = False, capture: bool = False):
    """Run shell command. Returns stdout str if capture=True, else returncode int."""
    kwargs: dict = {"shell": True, "timeout": timeout}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.DEVNULL
        kwargs["text"] = True
        result = subprocess.run(cmd, **kwargs)
        return result.stdout or ""
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

# ── Node.js 20 (required for patchright/playwright ≥ 1.49) ──────────────────
_node_ver = _run("node --version 2>/dev/null", capture=True, silent=True) or ""
if not str(_node_ver).strip().startswith("v2"):
    _p("  📦 Node.js < 20 detected — upgrading to Node 20 LTS…")
    _run("curl -fsSL https://deb.nodesource.com/setup_20.x | bash - > /dev/null 2>&1", timeout=60)
    _run("DEBIAN_FRONTEND=noninteractive apt-get install -y -q nodejs > /dev/null 2>&1", timeout=120)
    _p(f"  ✅ Node.js installed: {_run('node --version', capture=True, silent=True).strip()}")
else:
    _p(f"  ✅ Node.js OK: {str(_node_ver).strip()}")

_run("npm install -g npm@latest >/dev/null 2>&1", silent=True)
_run("mkdir -p /opt/xio_workflows", silent=True)
_run("npm install --prefix /opt/xio_workflows patchright otpauth >/dev/null 2>&1", silent=True)
_run(f"curl -sf {ASSET_BASE}/api/v1/workers/google-signin.mjs -o /opt/xio_workflows/google-signin.mjs", silent=True)
_p("  ✅ Node.js workflows & patchright ready")

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

# ── Chrome 131 install (optimal for UC 3.5.5 stealth — bypasses Chrome 153 detection) ──
# Priority: Drive cache → direct download.  Skipped if already installed.
_CHROME131_BIN = "/opt/chrome131/chrome"
if not os.path.isfile(_CHROME131_BIN):
    _p("  🔽 Chrome 131 not found — installing (UC 3.5.5 optimal version)…")
    _CHROME131_DRIVE_TAR = f"{ASSET_BASE_DRIVE}/cache/chrome131.tar.gz" if \
        "ASSET_BASE_DRIVE" in dir() else None
    _chrome131_installed = False

    # Try Drive cache first (fast, no bandwidth cost)
    _chrome131_drive_path = "/content/drive/MyDrive/XIOSYNC-Shared/cache/chrome131.tar.gz"
    if os.path.isfile(_chrome131_drive_path):
        _p("    ⚡ Chrome 131 Drive cache hit — extracting…")
        _run(f"mkdir -p /opt/chrome131 && tar -xzf '{_chrome131_drive_path}' -C /opt/chrome131 "
             f"--strip-components=1 2>&1 | tail -2", timeout=120)
        if os.path.isfile(_CHROME131_BIN):
            os.chmod(_CHROME131_BIN, 0o755)
            _chrome131_installed = True
            _p("    ✅ Chrome 131 installed from Drive cache")

    # Fallback: download from Google
    if not _chrome131_installed:
        _p("    ⬇️ Downloading Chrome 131 from dl.google.com…")
        _CHROME131_URL = (
            "https://storage.googleapis.com/chrome-for-testing-public"
            "/131.0.6778.204/linux64/chrome-linux64.zip"
        )
        _dl_rc = _run(
            f"curl -L --retry 3 -o /tmp/chrome131.zip '{_CHROME131_URL}' 2>&1 | tail -2",
            timeout=300,
        )
        if _dl_rc == 0 and os.path.exists("/tmp/chrome131.zip"):
            _run("mkdir -p /opt/chrome131 && "
                 "unzip -q /tmp/chrome131.zip -d /tmp/chrome131_src && "
                 "mv /tmp/chrome131_src/chrome-linux64/* /opt/chrome131/ 2>/dev/null || true && "
                 "rm -rf /tmp/chrome131.zip /tmp/chrome131_src",
                 timeout=120)
            if os.path.isfile(_CHROME131_BIN):
                os.chmod(_CHROME131_BIN, 0o755)
                _chrome131_installed = True
                # Cache to Drive for future boots
                _run(
                    f"tar -czf /tmp/chrome131.tar.gz -C /opt/chrome131 . && "
                    f"cp /tmp/chrome131.tar.gz "
                    f"'/content/drive/MyDrive/XIOSYNC-Shared/cache/chrome131.tar.gz' 2>/dev/null && "
                    f"rm /tmp/chrome131.tar.gz",
                    timeout=120,
                )
                _p("    ✅ Chrome 131 installed + cached to Drive")
            else:
                _p("    ⚠️  Chrome 131 extract failed — will use patchright Chromium 153 (detection risk higher)")
        else:
            _p("    ⚠️  Chrome 131 download failed — will use patchright Chromium 153")
else:
    _p(f"  ✅ Chrome 131 already at {_CHROME131_BIN}")

# Install matching chromedriver 131 (must match Chrome 131 version exactly)
_CD131_BIN = "/usr/local/bin/chromedriver131"
_CD131_VER = "131.0.6778.204"  # must match _CHROME131_URL version above
_CD131_UC_PATH = os.path.expanduser("~/.local/share/undetected_chromedriver/undetected_chromedriver")
_need_cd131 = (
    not os.path.isfile(_CD131_BIN) or
    _CD131_VER not in (subprocess.run([_CD131_BIN, "--version"],
                                      capture_output=True, text=True).stdout or "")
)
if _need_cd131 and os.path.isfile(_CHROME131_BIN):
    _p(f"  🔽 chromedriver {_CD131_VER} not found — installing…")
    _CD131_URL = (
        "https://storage.googleapis.com/chrome-for-testing-public/"
        f"{_CD131_VER}/linux64/chromedriver-linux64.zip"
    )
    _cd_rc = _run(
        f"curl -L --retry 3 -o /tmp/cd131.zip '{_CD131_URL}' 2>&1 | tail -2 && "
        f"unzip -q -o /tmp/cd131.zip -d /tmp/cd131_src && "
        f"mv /tmp/cd131_src/chromedriver-linux64/chromedriver {_CD131_BIN} && "
        f"chmod 755 {_CD131_BIN} && rm -rf /tmp/cd131.zip /tmp/cd131_src",
        timeout=60,
    )
    if _cd_rc == 0:
        _p(f"  ✅ chromedriver {_CD131_VER} installed at {_CD131_BIN}")
        # Also place at UC's path so it doesn't download a mismatched version
        os.makedirs(os.path.dirname(_CD131_UC_PATH), exist_ok=True)
        _run(f"cp {_CD131_BIN} {_CD131_UC_PATH} && chmod 755 {_CD131_UC_PATH}", timeout=5)
    else:
        _p("  ⚠️  chromedriver 131 install failed — UC will auto-download (may mismatch)")
elif os.path.isfile(_CD131_BIN):
    _p(f"  ✅ chromedriver {_CD131_VER} already at {_CD131_BIN}")
    # Ensure UC path is in sync
    os.makedirs(os.path.dirname(_CD131_UC_PATH), exist_ok=True)
    _run(f"cp {_CD131_BIN} {_CD131_UC_PATH} && chmod 755 {_CD131_UC_PATH}", timeout=5)


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

    # 3. Mount Drive — skip if already mounted in this session (re-run guard)
    _DRIVE_MOUNT = "/content/drive"
    if os.path.ismount(_DRIVE_MOUNT):
        _p(f"  ✅ Drive already mounted at {_DRIVE_MOUNT} — skipping re-mount")
        _drive_root = os.path.join(_DRIVE_MOUNT, "MyDrive", DRIVE_SHORTCUT_NAME)
        if not os.path.exists(_drive_root):
            _drive_root = os.path.join(_DRIVE_MOUNT, "MyDrive")
    else:
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


# ── Mesh identity helpers (defined at module scope — called inside else: block) ──
def _detect_runtime_type() -> str:
    """Detect runtime environment type."""
    if os.path.isdir("/content"):
        try:
            subprocess.run(["nvidia-smi"], capture_output=True, timeout=5, check=True)
            return "colab_gpu"
        except Exception:
            return "colab_cpu"
    if os.path.exists("/.dockerenv"):
        return "docker"
    return "vm"


def _resolve_mesh_identity(colab_account: str, xiosync_url: str, org_secret: str) -> str | None:
    """Resolve or create a stable MESH-{serial} binding for this runtime.
    colab_account is an optional hint (empty string is fine — serial is assigned by XIOSYNC)."""
    import urllib.request as _urq_mi  # noqa: PLC0415
    import json as _json_mi           # noqa: PLC0415
    try:
        _req = _urq_mi.Request(
            f"{xiosync_url}/api/v1/workers/mesh-identity",
            data=_json_mi.dumps({
                "colab_account": colab_account or "",   # optional hint
                "runtime_type": _detect_runtime_type(),
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Worker-Org-Secret": org_secret,
            },
            method="POST",
        )
        with _urq_mi.urlopen(_req, timeout=15) as _resp:
            _body = _json_mi.loads(_resp.read())
            _serial = _body.get("serial", 0)
            return f"MESH-{_serial:03d}"
    except Exception as _exc:
        _p(f"  ⚠️  Mesh identity resolution failed, using fallback: {_exc}")
        return None


if not TS_AUTH_KEY:
    _p("  ℹ️  No tailscale_auth_key in config — skipping Tailscale")
else:
    # 2. Kill any stale tailscaled daemon
    subprocess.run("pkill -9 tailscaled 2>/dev/null; sleep 1",
                   shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.makedirs(_TS_STATE_DIR, mode=0o700, exist_ok=True)

    # 2b. Detect Colab Google account — stable identity across runtimes
    _colab_account: str = ""
    try:
        import subprocess as _sp_ts  # noqa: PLC0415
        _gcloud_out = _sp_ts.check_output(
            ["gcloud", "config", "get-value", "account"],
            timeout=5, stderr=_sp_ts.DEVNULL
        ).decode().strip()
        if "@" in _gcloud_out:
            _colab_account = _gcloud_out.split("@")[0].lower().replace(".", "_")
    except Exception:
        pass

    if not _colab_account:
        try:
            import glob as _gl  # noqa: PLC0415
            for _cred_f in _gl.glob("/root/.config/gcloud/legacy_credentials/*/adc.json"):
                _acct = _cred_f.split("/")[-2]
                if "@" in _acct:
                    _colab_account = _acct.split("@")[0].lower().replace(".", "_")
                    break
        except Exception:
            pass

    # 2b. Mesh identity → stable MESH-{serial} hostname
    # Identity is based on XIOSYNC-assigned serial (domain-independent: works for
    # Colab runtimes, VMs, physical devices — no Google account required)
    _mesh_hostname = _resolve_mesh_identity(_colab_account, XIOSYNC_BASE, WORKER_SECRET) if (XIOSYNC_BASE and WORKER_SECRET) else None
    if _mesh_hostname:
        _NODE_IDENTITY = _mesh_hostname
        _p(f"  🔗  Mesh identity resolved: {_NODE_IDENTITY}")

    # 2c. TS state key — always MESH-serial based (domain-independent)
    _ts_key = f"ts_states/TS_{_NODE_IDENTITY}.state"
    _p(f"  🔑 TS state key: {_ts_key}")

    _ts_state_restored = False
    try:
        _state_bytes_restore: bytes | None = None
        if _XIO_FS is not None:
            _state_bytes_restore = _XIO_FS.get(_ts_key)
            _restore_source = "Drive FUSE"
        else:
            import urllib.request as _urq_ts  # noqa: PLC0415
            _restore_source = "none"
            try:
                _encoded_key = _ts_key.replace("/", "__")
                _ts_get_req = _urq_ts.Request(
                    f"{ASSET_BASE}/api/v1/workers/ts-state/{_encoded_key}",
                    headers={"X-Worker-Secret": WORKER_SECRET},
                    method="GET",
                )
                _raw = _urq_ts.urlopen(_ts_get_req, timeout=10).read()
                if _raw:
                    _state_bytes_restore = _raw
                    _restore_source = f"XIOSYNC API ({_ts_key.split('/')[-1]})"
            except Exception:
                pass

        if _state_bytes_restore:
            with open(_TS_STATE_FILE, "wb") as _sf:
                _sf.write(_state_bytes_restore)
            _ts_state_restored = True
            _p(f"  ✅ TS state restored from {_restore_source} "
               f"({len(_state_bytes_restore)} bytes) — will reconnect existing node")
        else:
            try:
                os.remove(_TS_STATE_FILE)
            except Exception:
                pass
            _p("  ℹ️  No saved TS state — fresh Tailscale login (new node will be created)")
    except Exception as _ts_restore_ex:
        try:
            os.remove(_TS_STATE_FILE)
        except Exception:
            pass
        _p(f"  ⚠️  TS state restore failed ({_ts_restore_ex}) — falling back to fresh auth")

    # 3. Start tailscaled
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

        # 4. Authenticate
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
            _ts_up_args.append("--reset")
        _ts_up = subprocess.run(_ts_up_args, capture_output=True, text=True, timeout=90)
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

        # 6. Save TS state: Drive FUSE (primary) → XIOSYNC API (fallback)
        if _ts_online and os.path.exists(_TS_STATE_FILE):
            try:
                import urllib.request as _urq  # noqa: PLC0415
                _state_bytes = open(_TS_STATE_FILE, "rb").read()

                if _XIO_FS is not None:
                    _written = _XIO_FS.put(_ts_key, _state_bytes, skip_if_same=True)
                    _p("  ✅ TS state saved to Drive FUSE" + (" (unchanged)" if not _written else ""))
                else:
                    _encoded_save = _ts_key.replace("/", "__")
                    _save_req = _urq.Request(
                        f"{ASSET_BASE}/api/v1/workers/ts-state/{_encoded_save}",
                        data=_state_bytes,
                        headers={"Content-Type": "application/octet-stream",
                                 "X-Worker-Secret": WORKER_SECRET},
                        method="PUT",
                    )
                    _urq.urlopen(_save_req, timeout=10)
                    _p(f"  ✅ TS state saved via XIOSYNC API ({_ts_key})")
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
    # level — proven to pass Google bot-detection where CDP-based approaches fail.
    # User directive: UC is the ONLY active browser engine in Colab.
    "undetected-chromedriver>=3.5.5",
    "pyotp>=2.9",              # TOTP-based 2FA for UC login
    "selenium>=4.18",
    # ── Agent / API framework ─────────────────────────────────────────────────
    "fastapi[standard]>=0.111",
    "uvicorn[standard]>=0.29",
    "httpx>=0.27",
    "boto3>=1.34",
    "pydantic>=2.7",
    "structlog>=24.1",
    "google-generativeai>=0.8",  # AIHealer Tier-10 Gemini LLM provider
    "Pillow>=10.0",              # JPEG screenshots via uc_snap()
    "patchright>=1.49.1",
     "psycopg[binary]>=3.1",     # async PG advisory locks in _execute_dag_run (P0-5)
]


# ── Drive cache fast-path: extract pre-bundled wheels from Drive FUSE ──────────
_PY_CACHE_KEY  = "cache/python/stealth-pkgs-v2.tar.gz"  # v2 includes pyotp + undetected-chromedriver
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


# ════════════════════════════════════════════════════════════════════════════════
# PHASE 2.5: Antigravity Auth Restore
# ════════════════════════════════════════════════════════════════════════════════
_p("\n" + "═" * 60)
_p("  Phase 2.5: Antigravity Auth Restore")
_p("═" * 60)

_agy_drive_root = _drive_root or "/content/drive/MyDrive/XIOSYNC-Shared"
_agy_cache_dir  = os.path.join(_agy_drive_root, "cache")
# Try account-bound backup first (agy-credentials-{email_slug}.tar.gz),
# fall back to the generic agy-credentials.tar.gz
import glob as _agy_glob
_acct_tars = sorted(_agy_glob.glob(os.path.join(_agy_cache_dir, "agy-credentials-*.tar.gz")))
if _acct_tars:
    _agy_creds_tar = _acct_tars[-1]  # most recently saved
    _p(f"  📎 Account-bound agy backup: {os.path.basename(_agy_creds_tar)}")
else:
    _agy_creds_tar = os.path.join(_agy_cache_dir, "agy-credentials.tar.gz")
if os.path.isfile(_agy_creds_tar):
    try:
        import tarfile as _agy_tf
        # Auto-detect tar format by peeking at first entry:
        #   New tars (saved by xiorun_agent ≥ commit 6885484): root is .gemini/antigravity-cli/
        #     → extract to /root/ so it lands at /root/.gemini/antigravity-cli/
        #   Old/stale tars: root is antigravity-cli/ (no .gemini/ prefix)
        #     → extract to /root/.gemini/ so it lands at /root/.gemini/antigravity-cli/
        with _agy_tf.open(_agy_creds_tar, "r:gz") as _agy_peek:
            _first = next(iter(_agy_peek)).name
        _extract_to = "/root/" if _first.startswith(".gemini") else "/root/.gemini/"
        os.makedirs(_extract_to, exist_ok=True)
        _run(f"tar xzf {_agy_creds_tar} -C {_extract_to}", silent=True)
    except Exception as _agy_ex:
        _p(f"  ⚠️  agy tar peek failed: {_agy_ex} — extracting to /root/.gemini/")
        os.makedirs("/root/.gemini", exist_ok=True)
        _run(f"tar xzf {_agy_creds_tar} -C /root/.gemini/", silent=True)
    _agy_test = _run("PATH=/root/.local/bin:/usr/local/bin:$PATH agy --version 2>&1", capture=True, silent=True)
    if _agy_test and "authentication required" not in str(_agy_test):
        _p(f"  ✅ agy auth restored ({_agy_test.strip().split(chr(10))[0]})")
    else:
        _p(f"  ⚠️  agy auth restore failed or token invalid — will need OAuth")
else:
    _p(f"  ⚠️  no agy credentials at {_agy_creds_tar} — will need OAuth")


# ════════════════════════════════════════════════════════════════════════════════
# PHASE 3: Browser readiness check (UC uses system Chrome — no download needed)
# ════════════════════════════════════════════════════════════════════════════════
_p("\n" + "═" * 60)
_p("  Phase 3: Chrome / UC readiness")
_p("═" * 60)

# UC (undetected-chromedriver) patches the system Chrome binary at launch —
# it does NOT need a separate Chromium download. Colab's runtime ships with
# Google Chrome pre-installed at /usr/bin/google-chrome.
_UC_CHROME_PATHS = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    # Colab / UC auto-downloaded chrome locations
    "/root/.local/share/undetected_chromedriver/google-chrome",
    "/root/.local/share/undetected_chromedriver",
]
# Also search via 'which' and common snap/opt paths
_uc_chrome_found = next((p for p in _UC_CHROME_PATHS if os.path.isfile(p)), None)
if not _uc_chrome_found:
    import shutil as _sh  # noqa: PLC0415
    _uc_chrome_found = _sh.which("google-chrome") or _sh.which("chromium-browser") or _sh.which("chromium")

if _uc_chrome_found:
    try:
        _chrome_ver = subprocess.check_output(
            [_uc_chrome_found, "--version"], timeout=5,
            stderr=subprocess.DEVNULL
        ).decode().strip()
        _p(f"  ✅ System Chrome ready: {_chrome_ver} → {_uc_chrome_found}")
    except Exception:
        _p(f"  ✅ System Chrome found at {_uc_chrome_found}")
else:
    # UC (undetected_chromedriver) automatically downloads and patches Chrome
    # on the first driver launch — nothing to do here at boot time.
    # The download takes ~15-30s and is transparent to the caller.
    _p("  ℹ️  System Chrome not at standard paths — UC will auto-download on first use")

# ── Patchright patched Chromium ──────────────────────────────────
# Phase 3b-pre: Restore patchright cache from Drive (saves ~15-30s download)
_PATCHRIGHT_CACHE_TAR = os.path.join(_drive_root or "/content/drive/MyDrive/XIOSYNC-Shared", "cache", "patchright-cache.tar.gz")
if os.path.isfile(_PATCHRIGHT_CACHE_TAR):
    try:
        import tarfile as _pr_tf
        _p("  📦 Restoring patchright cache from Drive...")
        with _pr_tf.open(_PATCHRIGHT_CACHE_TAR, "r:gz") as _pr_tar:
            _pr_tar.extractall(path="/root/.cache")
        _p(f"  ✅ Patchright cache restored from Drive ({os.path.getsize(_PATCHRIGHT_CACHE_TAR)//1024//1024}MB)")
    except Exception as _pr_restore_err:
        _p(f"  ⚠️  Patchright cache restore failed (non-fatal): {_pr_restore_err}")
else:
    _p("  ℹ️  No patchright Drive cache found — will download fresh")

_PR_BOOT_URL  = f"{ASSET_BASE}/api/v1/workers/patchright-boot.py"
_PR_BOOT_PATH = "/tmp/patchright_boot.py"
try:
    import urllib.request as _urq  # noqa
    _urq.urlretrieve(_PR_BOOT_URL, _PR_BOOT_PATH)
    # exec into current namespace so setup_patchright() is available
    with open(_PR_BOOT_PATH) as _pbf:
        exec(compile(_pbf.read(), _PR_BOOT_PATH, "exec"), globals())  # noqa
    _pr_cache = setup_patchright()  # type: ignore[name-defined]  # noqa: F821
    _p(f"  ✅ Phase 3b: Patchright ready ({_pr_cache})")
except Exception as exc:
    _p(f"  ⚠️  Patchright setup failed (non-fatal): {exc}")







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
        # Chrome location (UC auto-downloaded or system Chrome)
        "PLAYWRIGHT_BROWSERS_PATH": os.path.expanduser("~/.cache/ms-patchright"),
        # ── SSH SOCKS5 exit node: route Chrome through Mac's residential IP ───
        # XIORUN_PROXY_SSH_HOST = Mac Tailscale IP (100.86.149.127)
        # Agent will SSH -D to this host on startup to create socks5://127.0.0.1:19056
        "XIORUN_PROXY_SSH_HOST":    C.get("proxy_ssh_host", ""),
        "XIORUN_PROXY_SSH_USER":    C.get("proxy_ssh_user", "karmareturns"),
        "XIORUN_PROXY_SSH_KEY":     "/root/.ssh/xio_proxy_key",
        "XIORUN_PROXY_LOCAL_PORT":  "19056",  # 1055 conflicts with tailscaled userspace
        # ── WS SOCKS5 bridge (primary — bypasses Tailscale ACL via XIOSYNC HTTPS) ──
        "XIORUN_INTERNAL_SECRET":   INTERNAL_SECRET,
        "XIORUN_PPPOE_PROXY":       C.get("pppoe_proxy", "100.106.81.15:10001"),
        # ─────────────────────────────────────────────────────────────────────
        "DISPLAY":                  ":99",
    }

    # ── Write SSH proxy private key so agent can SSH to Mac ──────────────────
    _proxy_ssh_key_pem = C.get("proxy_ssh_key", "")
    if _proxy_ssh_key_pem:
        _proxy_key_path = "/root/.ssh/xio_proxy_key"
        with open(_proxy_key_path, "w") as _pkf:
            _pkf.write(_proxy_ssh_key_pem.strip() + "\n")
        os.chmod(_proxy_key_path, 0o600)
        _p("  ✅ SSH proxy key written → /root/.ssh/xio_proxy_key")
    _agent_log = open("/tmp/xiorun_agent.log", "w")
    _agent_proc = subprocess.Popen(
        [sys.executable, _AGENT_PATH],
        env=_agent_env,
        stdout=_agent_log,
        stderr=_agent_log,
    )
    atexit.register(lambda: _agent_proc.terminate() if _agent_proc else None)

    # Wait for agent to be ready (max 35s — agent starts noVNC + SOCKS5 + patchright at boot)
    _ready = False
    for _ in range(70):
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

# ════════════════════════════════════════════════════════════════════════════════
# PHASE 6: Post-boot Antigravity setup
# ════════════════════════════════════════════════════════════════════════════════
_p("\n" + "═" * 60)
_p("  Phase 6: Post-boot Antigravity setup")
_p("═" * 60)
_agy_test = _run("agy --version 2>&1", capture=True, silent=True)
if "authentication required" in str(_agy_test):
    _p("  ⚠️  agy auth missing — triggering browser login flow via /ai/install-agy")
    try:
        import urllib.request as _urq
        req = _urq.Request("http://127.0.0.1:9300/ai/install-agy", method="POST")
        _urq.urlopen(req, timeout=10)
    except Exception as e:
        _p(f"  ⚠️  Failed to trigger /ai/install-agy: {e}")
else:
    _p("  ✅ agy already authenticated")

_p(f"\n" + "═" * 60)
_p(f"  Boot complete ✅")
_p(f"  Node:          {NODE_NAME}")
_p(f"  Tailscale IP:  {_my_ts_ip or 'not connected'}")
_p(f"  XIOSYNC:       {XIOSYNC_BASE or BOOTSTRAP_XIOSYNC_BASE or 'not configured'}")
_p(f"  xiorun_agent:  :{XIORUN_PORT} (PID {_agent_proc.pid if _agent_proc else 'N/A'})")
_p(f"  Enrollment ID: {_ENROLLMENT_ID or 'not enrolled'}")
_p("═" * 60 + "\n")

