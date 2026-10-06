#!/usr/bin/env python3
"""
uc_login_runner.py — Standalone UC Chrome Google Sign-In runner.

Called by xiorun_agent.py /run-uc-login as a subprocess to avoid the
"cannot connect to chrome" crash that happens when uc.Chrome() is launched
from any thread inside the uvicorn/patchright agent process.

Usage:
    python3 uc_login_runner.py <params_json_path> <result_json_path>

Params JSON fields:
    email, password, totp_secret, proxy_url, user_agent,
    user_data_dir, exit_node_public_ip
"""
import sys, os, json, logging, time, socket, shutil, tempfile, re, base64

# ── Environment ────────────────────────────────────────────────────────────
os.environ.setdefault("DISPLAY", ":99")
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/root/.cache/ms-patchright")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s uc_runner %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("uc_runner")

# ── Constants ──────────────────────────────────────────────────────────────
CHROME_BIN      = "/opt/chrome131/chrome"
CHROMEDRIVER    = "/usr/local/bin/chromedriver131"
CHROME_VERSION  = 131

def _pick_chromedriver(version: int) -> str:
    """Pick the best matching chromedriver for the given Chrome major version."""
    candidates = [
        f"/usr/local/bin/chromedriver{version}",
        f"/usr/local/bin/chromedriver",
        CHROMEDRIVER,
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return CHROMEDRIVER

STEALTH_EXT_DIR = "/tmp/xio_stealth_ext"
AGENT_BASE      = "http://127.0.0.1:9300"


def _free_port() -> int:
    s = socket.socket(); s.bind(("", 0)); p = s.getsockname()[1]; s.close(); return p


def _socks5_get(host, port, target_host, target_port, timeout=8) -> bytes:
    """Raw SOCKS5 GET request for IP resolution."""
    import struct
    s = socket.create_connection((host, port), timeout=timeout)
    s.sendall(b"\x05\x01\x00")
    assert s.recv(2) == b"\x05\x00"
    hn = target_host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(hn)]) + hn + struct.pack(">H", target_port))
    resp = s.recv(10)
    assert resp[1] == 0, f"SOCKS5 connect failed: {resp[1]}"
    s.sendall(f"GET / HTTP/1.0\r\nHost: {target_host}\r\n\r\n".encode())
    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk: break
        data += chunk
    s.close()
    return data


def _get_exit_ip(proxy_host="127.0.0.1", proxy_port=19055) -> str | None:
    try:
        resp = _socks5_get(proxy_host, proxy_port, "api.ipify.org", 80, timeout=8)
        body = resp.split(b"\r\n\r\n", 1)[-1].decode().strip()
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", body):
            return body
    except Exception as e:
        logger.warning(f"exit IP check failed: {e}")
    return None


def _get_stealth_js() -> str | None:
    """Try to fetch stealth JS from the running agent."""
    try:
        import urllib.request
        r = urllib.request.urlopen(f"{AGENT_BASE}/debug/stealth-js", timeout=5)
        return r.read().decode()
    except Exception as e:
        logger.warning(f"stealth JS fetch failed: {e}")
        # Fall back to reading from disk
        ext_stealth = os.path.join(STEALTH_EXT_DIR, "stealth.js")
        if os.path.isfile(ext_stealth):
            with open(ext_stealth) as f:
                return f.read()
    return None


def _ensure_stealth_ext() -> str | None:
    """Write/refresh the stealth extension; return the directory path or None."""
    js = _get_stealth_js()
    if not js:
        return None
    os.makedirs(STEALTH_EXT_DIR, exist_ok=True)
    manifest = json.dumps({
        "manifest_version": 3, "name": "XIO Stealth", "version": "1.0",
        "content_scripts": [{
            "matches": ["<all_urls>"], "js": ["stealth.js"],
            "run_at": "document_start", "all_frames": True, "world": "MAIN"
        }]
    })
    with open(os.path.join(STEALTH_EXT_DIR, "manifest.json"), "w") as f:
        f.write(manifest)
    with open(os.path.join(STEALTH_EXT_DIR, "stealth.js"), "w") as f:
        f.write(js)
    logger.info(f"stealth ext: {len(js)} bytes → {STEALTH_EXT_DIR}")
    return STEALTH_EXT_DIR


def run_uc_login(params: dict) -> dict:
    import undetected_chromedriver as uc
    import pyotp

    email        = params["email"]
    password     = params["password"]
    totp_secret  = params.get("totp_secret", "")
    proxy_url    = params.get("proxy_url") or None
    user_agent   = params.get("user_agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
    exit_ip_expected = params.get("exit_node_public_ip") or None

    # ── Profile dir: ALWAYS use a dedicated login profile ─────────────────
    # The agent passes the main PRFL dir (e.g. PRFL_970d...) which is already
    # held by Chrome 131 (lifespan browser). Chrome 154 cannot open the same
    # profile concurrently → SingletonLock dialog blocks CDP binding.
    # Solution: always use a separate /tmp/uc_login_<email_base> dir for UC.
    _email_base = email.split("@")[0].replace(".", "_").replace("+", "_")
    user_data_dir = f"/tmp/uc_login_{_email_base}"

    os.makedirs(user_data_dir, exist_ok=True)

    # ── Remove stale Chrome singleton locks ────────────────────────────────
    # If a previous run crashed, Chrome left lock files that cause a blocking dialog.
    # The dialog pauses Chrome before it binds its CDP port → chromedriver timeout.
    # Clean locks from the specific profile AND from any temp Chrome lock dirs.
    import glob as _glob
    _lock_dirs = [user_data_dir] + _glob.glob("/tmp/com.google.Chrome.*") + _glob.glob("/tmp/.org.chromium.Chromium.*")
    for _ld in _lock_dirs:
        for _lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            _lp = os.path.join(_ld, _lock)
            if os.path.exists(_lp):
                logger.info(f"Removing stale lock: {_lp}")
                try:
                    os.remove(_lp)
                except Exception:
                    pass

    # ── Proxy detection ────────────────────────────────────────────────────
    proxy_host, proxy_port = "127.0.0.1", 19055
    if proxy_url:
        m = re.match(r"socks5h?://([^:]+):(\d+)", proxy_url)
        if m:
            proxy_host, proxy_port = m.group(1), int(m.group(2))
    logger.info(f"proxy: {proxy_host}:{proxy_port}")

    # ── Exit IP ────────────────────────────────────────────────────────────
    exit_ip = _get_exit_ip(proxy_host, proxy_port)
    logger.info(f"exit IP: {exit_ip}")

    # ── Stealth extension ──────────────────────────────────────────────────
    ext_dir = _ensure_stealth_ext()

    # ── ChromeOptions ──────────────────────────────────────────────────────
    opts = uc.ChromeOptions()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    # Colab kernel rejects Chrome's memfd_create() without MFD_EXEC flag.
    # --disable-seccomp-filter-sandbox prevents Chrome's renderer from hitting
    # the seccomp policy that blocks memfd_create, letting Chrome bind its CDP port.
    opts.add_argument("--disable-seccomp-filter-sandbox")
    opts.add_argument("--no-zygote")
    opts.add_argument("--no-error-dialogs")   # suppress profile-lock dialog that blocks CDP port binding
    # NOTE: Do NOT use --single-process or --disable-gpu — both are strong bot signals.
    # --single-process: detectable via SharedArrayBuffer/Worker behavior differences.
    # --disable-gpu: kills WebGL → obvious fingerprint gap even with stealth extension.
    opts.add_argument("--use-gl=angle")
    opts.add_argument("--use-angle=gl")
    opts.add_argument("--enable-webgl")
    opts.add_argument("--enable-webgl2")
    opts.add_argument("--ignore-gpu-blocklist")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--renderer-process-limit=4")
    opts.add_argument(f"--user-agent={user_agent}")
    opts.add_argument("--lang=en-IN")
    opts.add_argument("--remote-allow-origins=*")
    if proxy_url:
        opts.add_argument(f"--proxy-server=socks5://{proxy_host}:{proxy_port}")
    if ext_dir:
        opts.add_argument(f"--load-extension={ext_dir}")
    opts.add_experimental_option("prefs", {
        "webrtc.ip_handling_policy":      "disable_non_proxied_udp",
        "webrtc.multiple_routes_enabled": False,
        "webrtc.nonproxied_udp_enabled":  False,
    })
    # Prefer google-chrome-stable over Chrome for Testing
    _chrome_bin = CHROME_BIN
    _chrome_ver = CHROME_VERSION
    for _stable_path in [
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/opt/google/chrome/google-chrome",
    ]:
        if os.path.isfile(_stable_path):
            _chrome_bin = _stable_path
            try:
                import subprocess as _sp
                _v = _sp.check_output([_stable_path, "--version"], stderr=_sp.DEVNULL, timeout=5).decode()
                _chrome_ver = int(_v.strip().split()[-1].split(".")[0])
                logger.info(f"Using google-chrome-stable: {_stable_path} (v{_chrome_ver})")
            except Exception:
                _chrome_ver = 131
            break
    else:
        logger.info(f"google-chrome-stable not found, using Chrome for Testing: {_chrome_bin}")

    port = _free_port()

    # ── Per-run chromedriver copy ──────────────────────────────────────────
    # UC patches the chromedriver binary and caches it at a global path.
    # When concurrent agent retries run, they corrupt each other's cached binary.
    import shutil as _shutil
    _src_cd = _pick_chromedriver(_chrome_ver)
    _run_cd = f"/tmp/uc_cd_run_{port}"
    _shutil.copy2(_src_cd, _run_cd)
    os.chmod(_run_cd, 0o755)
    logger.info(f"Per-run chromedriver: {_run_cd} (from {_src_cd})")

    # ── Manual Chrome launch with session isolation ────────────────────────
    # Chrome is launched directly with start_new_session=True + close_fds=True
    # to avoid inheriting the agent's open file descriptors.
    import subprocess as _sp

    # Args we set manually — filter these out of opts.arguments to avoid dupes
    _prefix_flags = {"--remote-debugging-port", "--remote-debugging-address",
                     "--remote-allow-origins", "--user-data-dir"}
    _extra_opts = [
        a for a in opts.arguments
        if not any(a.startswith(f) for f in _prefix_flags)
        # --no-zygote disables Chrome's zygote process which is needed for SOCKS5
        and a != "--no-zygote"
    ]

    _chrome_args = [
        _chrome_bin,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_data_dir}",
    ] + _extra_opts

    _chrome_env = {
        "DISPLAY": os.environ.get("DISPLAY", ":99"),
        "HOME":    os.environ.get("HOME", "/root"),
        "PATH":    os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "XAUTHORITY": os.environ.get("XAUTHORITY", ""),
    }

    _proxy_in_args = any("proxy-server" in a for a in _chrome_args)
    logger.info(f"Chrome args preview: proxy={_proxy_in_args} no-zygote=False args={len(_chrome_args)}")
    logger.info(f"launching Chrome manually on port {port}...")
    _chrome_proc = _sp.Popen(
        _chrome_args,
        stdout=_sp.DEVNULL,
        stderr=_sp.DEVNULL,
        env={k: v for k, v in _chrome_env.items() if v},
        close_fds=True,
        start_new_session=True,   # gives Chrome its own process group
    )
    logger.info(f"Chrome PID: {_chrome_proc.pid}, waiting for CDP port {port}...")

    # Wait for Chrome to be ready (poll /json/version)
    import urllib.request as _ureq
    _t0 = time.time()
    while time.time() - _t0 < 30:
        try:
            _r = _ureq.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
            _ver = json.loads(_r.read())
            logger.info(f"Chrome ready: {_ver.get('Browser')} on port {port}")
            break
        except Exception:
            time.sleep(0.5)
    else:
        _chrome_proc.kill()
        raise RuntimeError(f"Chrome did not become ready on port {port} within 30s")

    # Now attach UC via CDP
    logger.info(f"attaching UC to Chrome on port {port}...")
    driver = uc.Chrome(
        options=opts,
        browser_executable_path=_chrome_bin,
        driver_executable_path=_run_cd,
        version_main=_chrome_ver,
        use_subprocess=False,      # ← DON'T let UC spawn Chrome; we already did
        headless=False,
        port=port,
        user_data_dir=user_data_dir,
        keep_user_data_dir=True,
    )
    logger.info(f"UC Chrome attached! title={driver.title}")

    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.keys import Keys
        import random as _rand

        wait = WebDriverWait(driver, 40)

        # ── Warm-up skipped: SOCKS5 proxy (127.0.0.1:19055) is shared with ──
        # Chrome 131 (main browser). When proxy is at capacity, Chrome 154's
        # warm-up browsing (google.com → news → search) gets ERR_SOCKS_CONNECTION_FAILED.
        # Skip warm-up and go directly to sign-in. Re-enable once login works.
        logger.info("skipping warm-up (proxy capacity), going direct to sign-in...")

        # ── Navigate to Google sign-in ─────────────────────────────────────
        logger.info("navigating to Google sign-in...")
        driver.get("https://accounts.google.com/signin/v2/identifier?hl=en&flowName=GlifWebSignIn")
        time.sleep(_rand.uniform(2.5, 4.0))

        # ── Email ──────────────────────────────────────────────────────────
        logger.info(f"typing email: {email}")
        email_field = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='identifier'], #identifierId")))
        # Click the field naturally before typing
        email_field.click()
        time.sleep(_rand.uniform(0.3, 0.7))
        email_field.clear()
        time.sleep(_rand.uniform(0.2, 0.4))
        # Human-like typing: variable delay per keystroke
        for ch in email:
            email_field.send_keys(ch)
            time.sleep(_rand.uniform(0.04, 0.12))
        time.sleep(_rand.uniform(0.8, 1.5))
        email_field.send_keys(Keys.ENTER)
        logger.info("email submitted")
        time.sleep(_rand.uniform(3.5, 5.0))

        logger.info(f"URL after email: {driver.current_url}")
        # Check for immediate rejection
        if "rejected" in driver.current_url or "Couldn" in driver.title:
            logger.error(f"Google rejected after email! URL={driver.current_url}")
            return {"ok": False, "error": f"Google rejected login: {driver.current_url}", "uc_port": port}

        # ── Password ────────────────────────────────────────────────────────
        logger.info("waiting for password field...")
        pwd_field = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password']")))
        pwd_field.click()
        time.sleep(_rand.uniform(0.3, 0.6))
        for ch in password:
            pwd_field.send_keys(ch)
            time.sleep(_rand.uniform(0.05, 0.13))
        time.sleep(_rand.uniform(0.7, 1.2))
        pwd_field.send_keys(Keys.ENTER)
        logger.info("password submitted")
        time.sleep(_rand.uniform(4.0, 5.5))

        logger.info(f"URL after password: {driver.current_url}")

        # ── TOTP ────────────────────────────────────────────────────────────
        url = driver.current_url
        if totp_secret and ("challenge" in url or "totp" in url.lower() or "verification" in url.lower() or "mfa" in url.lower()):
            totp_code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
            logger.info(f"TOTP challenge: code={totp_code}")
            totp_field = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='tel'], input[name='totpPin'], input[id*='totp']")))
            for ch in totp_code:
                totp_field.send_keys(ch)
                time.sleep(0.05)
            time.sleep(0.5)
            totp_field.send_keys(Keys.ENTER)
            logger.info("TOTP submitted")
            time.sleep(4)
            logger.info(f"URL after TOTP: {driver.current_url}")

        # ── Verify ─────────────────────────────────────────────────────────
        time.sleep(3)
        final_url = driver.current_url
        logger.info(f"final URL: {final_url}")

        cookies = driver.get_cookies()
        google_cookies = [c for c in cookies if "google" in c.get("domain", "")]
        logger.info(f"Google cookies: {len(google_cookies)}")

        ok = len(google_cookies) >= 3 and "signin" not in final_url and "rejected" not in final_url

        return {
            "ok":        ok,
            "uc_port":   port,
            "cookies":   cookies,
            "url":       final_url,
            "exit_ip":   exit_ip,
            "error":     None if ok else f"Login failed — final URL: {final_url}",
        }

    except Exception as e:
        logger.error(f"login error: {e}", exc_info=True)
        try:
            ss = driver.get_screenshot_as_base64()
            with open("/tmp/uc_login_debug.png", "wb") as f:
                f.write(base64.b64decode(ss))
            logger.info("screenshot saved to /tmp/uc_login_debug.png")
        except: pass
        try:
            _chrome_proc.kill()
        except: pass
        return {"ok": False, "error": str(e), "uc_port": port}
    # NOTE: driver is intentionally NOT quit() here — the agent needs the
    # cookies from this Chrome session and will close it later.


# ── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: uc_login_runner.py <params.json> <result.json>", file=sys.stderr)
        sys.exit(2)

    params_path = sys.argv[1]
    result_path = sys.argv[2]

    with open(params_path) as f:
        params = json.load(f)

    try:
        result = run_uc_login(params)
    except Exception as e:
        logger.error(f"top-level error: {e}", exc_info=True)
        result = {"ok": False, "error": str(e)}

    with open(result_path, "w") as f:
        json.dump(result, f)

    logger.info(f"result written to {result_path}: ok={result.get('ok')}")
    sys.exit(0 if result.get("ok") else 1)
