"""Antigravity installation and credential restoration."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil

logger = logging.getLogger('xiorun_agent')

DRIVE_ROOT = os.environ.get('XIO_DRIVE_ROOT', '/content/drive/MyDrive/XIOSYNC-Shared')

try:
    from agent.ai.agy_cli import _is_agy_installed, _is_agy_authenticated, _AGY_BIN
except ImportError:
    try:
        from agent.ai.agy_cli import _is_agy_installed, _is_agy_authenticated, _AGY_BIN
    except ImportError:
        pass

# Fallback imports from xiorun_agent if available
def _resolve_ip_geo(ip):
    try:
        import xiorun_agent as _agent
        return _agent._resolve_ip_geo(ip)
    except Exception:
        return {'timezone': 'America/New_York', 'country': 'US'}

def _get_runtime_public_ip():
    try:
        import xiorun_agent as _agent
        return _agent._get_runtime_public_ip()
    except Exception:
        return None

def _load_profile_fingerprint(pid, root):
    try:
        import xiorun_agent as _agent
        return _agent._load_profile_fingerprint(pid, root)
    except Exception:
        return {}

def _find_patchright_chromium():
    import glob
    candidates = sorted(glob.glob('/root/.cache/ms-patchright/chromium-*/chrome-linux64/chrome'), reverse=True)
    return candidates[0] if candidates else None

_PATCHRIGHT_CHROME = _find_patchright_chromium()
_STEALTH_UA_CHROME = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.108 Safari/537.36'

try:
    import xiorun_agent as _agent
    _STEALTH_JS = _agent._STEALTH_JS
except Exception:
    _STEALTH_JS = ''

async def ai_install_agy() -> dict:
    """Install agy binary and authenticate using a persisted Google Chrome profile.

    Phase 4 — Native Colab agy path:
    1. Install agy binary (from Drive cache or official installer)
    2. Restore Chrome profile from Drive profiles/ (contains active Google session)
    3. Suspend Chrome CDP so agy doesn't enter 'local chrome mode'
    4. Run agy auth PTY flow — Chrome opens, session already active, OAuth auto-consents
    5. Persist ~/.gemini/antigravity-cli/ credentials to Drive
    """
    import gzip as _gz
    import shutil as _sh
    import subprocess as _sp

    result: dict = {
        "binary_installed": False,
        "binary_source": None,
        "auth_attempted": False,
        "auth_success": False,
        "profile_restored": False,
        "creds_persisted": False,
        "error": None,
    }

    # ── Step 1: Install binary ──────────────────────────────────────────────────
    _drive_binary = os.path.join(DRIVE_ROOT, "cache/agy-binary.gz")

    if not _is_agy_installed():
        # Try Drive cache first (fast — no network)
        if os.path.isfile(_drive_binary):
            try:
                os.makedirs(os.path.dirname(_AGY_BIN), exist_ok=True)
                with _gz.open(_drive_binary, "rb") as f_in, open(_AGY_BIN, "wb") as f_out:
                    _sh.copyfileobj(f_in, f_out)
                os.chmod(_AGY_BIN, 0o755)
                result["binary_source"] = "drive_cache"
                logger.info("ai_install_agy: binary restored from Drive cache")
            except Exception as e:
                logger.warning(f"ai_install_agy: Drive restore failed: {e}")

        # Official installer fallback
        if not _is_agy_installed():
            try:
                env = {**os.environ, "HOME": os.path.expanduser("~")}
                out = _sp.check_output(
                    "curl -fsSL https://antigravity.google/cli/install.sh | bash",
                    shell=True,
                    text=True,
                    timeout=180,
                    env=env,
                    stderr=_sp.STDOUT,
                )
                result["binary_source"] = "official_installer"
                logger.info("ai_install_agy: installed from official installer")
            except Exception as e:
                result["error"] = f"Binary install failed: {e}"
                return result

    if _is_agy_installed():
        result["binary_installed"] = True
        # Cache to Drive for next boot
        if not os.path.isfile(_drive_binary):
            try:
                os.makedirs(os.path.dirname(_drive_binary), exist_ok=True)
                with open(_AGY_BIN, "rb") as f_in, _gz.open(_drive_binary, "wb") as f_out:
                    f_out.write(f_in.read())
                logger.info(f"ai_install_agy: binary cached to Drive {_drive_binary}")
            except Exception as cache_err:
                logger.warning(f"ai_install_agy: Drive cache failed: {cache_err}")
    else:
        result["error"] = f"Binary not found at {_AGY_BIN} after install attempt"
        return result

    # Already authenticated? Short-circuit.
    if _is_agy_authenticated():
        result["auth_success"] = True
        result["auth_attempted"] = False
        return result

    # ── Step 2.5: Restore agy credentials from Drive cache ──────────────────────
    # The archive stores tokens under .gemini/antigravity-cli/... (arcname prefix).
    # Extract to HOME (/root) so entries unpack as /root/.gemini/antigravity-cli/...
    # Do NOT extract to ~/.gemini or paths become ~/.gemini/.gemini/... (double-nest).
    _agy_creds_cache = os.path.join(DRIVE_ROOT, "cache/agy-credentials.tar.gz")
    if os.path.isfile(_agy_creds_cache):
        try:
            _home_dir = os.path.expanduser("~")
            os.makedirs(os.path.join(_home_dir, ".gemini"), exist_ok=True)
            # Extract to HOME — arcname=".gemini/{item}" → /root/.gemini/{item}
            _sp.run(
                ["tar", "xzf", _agy_creds_cache, "-C", _home_dir],
                capture_output=True,
                timeout=30,
            )
            logger.info(
                f"ai_install_agy: credentials restored from Drive cache → {os.path.join(_home_dir, '.gemini')}"
            )
            if _is_agy_authenticated():
                result["auth_success"] = True
                result["auth_attempted"] = False
                result["creds_persisted"] = True  # already on Drive
                logger.info("ai_install_agy: auth confirmed from restored credentials ✅")
                return result
            else:
                logger.info(
                    "ai_install_agy: credentials restored but tokens not valid (expired) — proceeding with OAuth"
                )
        except Exception as _creds_err:
            logger.warning(f"ai_install_agy: credentials restore failed: {_creds_err}")

    # agy's OAuth opens a browser; pre-loading a profile with Google cookies
    # means the consent screen auto-fills and redirects back immediately.
    _profiles_drive = os.path.join(DRIVE_ROOT, "profiles")
    _restore_dir = None

    if os.path.isdir(_profiles_drive):
        _profile_tars = sorted(
            [
                p
                for p in os.listdir(_profiles_drive)
                if p.startswith("PRFL-") and p.endswith(".tar.gz")
            ],
            reverse=True,
        )  # highest serial first (most recent login)
        if _profile_tars:
            # Prefer a profile with a valid fingerprint JSON (email present)
            _tar_path = os.path.join(_profiles_drive, _profile_tars[0])
            for _pt in _profile_tars:
                _fp_path = os.path.join(
                    _profiles_drive, _pt.replace(".tar.gz", ".fingerprint.json")
                )
                if os.path.isfile(_fp_path):
                    try:
                        import json as _fp_j

                        _fp_data = _fp_j.load(open(_fp_path))
                        if "@" in _fp_data.get("email", ""):
                            _tar_path = os.path.join(_profiles_drive, _pt)
                            logger.info(
                                f"ai_install_agy: using profile {_pt} ({_fp_data['email']}) for OAuth browser"
                            )
                            break
                    except Exception:
                        pass
            _base_restore = "/tmp/agy-auth-chrome-profile"
            try:
                import tarfile as _tf_agy

                if os.path.exists(_base_restore):
                    _sh.rmtree(_base_restore)
                os.makedirs(_base_restore, exist_ok=True)
                with _tf_agy.open(_tar_path, "r:gz") as tf:
                    # Skip Chrome runtime artifacts (singleton sockets, lock files)
                    # that tar rejects as unsafe absolute symlinks.
                    _skip = {
                        "SingletonSocket",
                        "SingletonLock",
                        "SingletonCookies",
                        "DevToolsActivePort",
                        "lockfile",
                    }
                    for _member in tf.getmembers():
                        if any(_member.name.endswith(s) for s in _skip):
                            continue
                        try:
                            tf.extract(_member, path="/tmp", filter="tar")
                        except Exception:
                            pass  # skip unextractable members silently
                # Tarball contains a single PRFL_* dir — locate it
                _children = sorted(
                    [
                        c
                        for c in os.listdir("/tmp")
                        if c.startswith("PRFL_") and os.path.isdir(f"/tmp/{c}")
                    ]
                )
                _restore_dir = f"/tmp/{_children[0]}" if _children else None
                result["profile_restored"] = True
                result["profile_tar"] = _profile_tars[0]
                logger.info(f"ai_install_agy: restored profile {_profile_tars[0]} → {_restore_dir}")
            except Exception as pe:
                logger.warning(f"ai_install_agy: profile restore failed: {pe}")
        else:
            logger.warning("ai_install_agy: no PRFL-*.tar.gz found in Drive profiles/")
    else:
        logger.warning(f"ai_install_agy: Drive profiles/ not at {_profiles_drive}")

    # ── Step 4: Drive agy auth via tmux + ProfileManager browser ────────────────
    # agy renders its login flow as a fullscreen TUI — OAuth URL is painted via
    # cursor-positioning ANSI.  tmux -J captures the rendered cell grid so we can
    # read the URL as plain text.  Browser driving is delegated to ProfileManager
    # which pre-injects the full anti-detection fingerprint for this profile.
    #
    # Flow:
    #   a) agy in tmux → "Select login method" menu → send Enter (Google OAuth)
    #   b) agy prints OAuth URL → ProfileManager.launch_in_thread() drives browser
    #      with profile cookies + stealth JS + residential SOCKS5 proxy
    #   c) Google auto-consents (active session) → redirect to antigravity.google
    #   d) Extract auth code → send to agy tmux input
    #   e) agy writes tokens → "signed in" marker detected
    result["auth_attempted"] = True
    _auth_timeout = 240  # 4 min total

    import re as _re_auth
    import time as _time

    # Determine profile_id from the profile_tar we restored
    _profile_id = None
    if result.get("profile_tar"):
        _m_prfl = _re_auth.match(r"(PRFL-\d+)\.tar\.gz", result["profile_tar"])
        if _m_prfl:
            _profile_id = _m_prfl.group(1)
    if not _profile_id:
        _profile_id = "PRFL-001"

    # Load persistent fingerprint for this profile (or generate + save to Drive).
    # Returns the same dict format the existing launch_browser() machinery uses.
    _fp = _load_profile_fingerprint(_profile_id, DRIVE_ROOT)
    logger.info(f"agy_auth: fingerprint for {_profile_id}: ua={_fp.get('ua_template', '')[:40]}...")

    # Proxy is passed as --proxy-server= Chrome flag inside _run_oauth_browser()
    # (matching how the existing launch_browser() does it).
    # The WS SOCKS5 bridge on :19055 must be running (started in lifespan).
    _proxy_cfg = None  # not used here — handled inside the thread via Chrome flag

    # Geo: resolve from the worker's actual public IP for TZ/locale consistency
    _exit_ip = _get_runtime_public_ip()
    _geo = _resolve_ip_geo(_exit_ip)

    import datetime as _dt

    try:
        import zoneinfo as _zi

        _tz_obj = _zi.ZoneInfo(_geo["timezone"])
        _tz_offset = -int(_dt.datetime.now(_tz_obj).utcoffset().total_seconds() // 60)
    except Exception:
        _tz_offset = 0
    _screen_h = str(_fp.get("height", 1080))
    _session_stealth_js = (
        _STEALTH_JS.replace("'{TIMEZONE}'", f"'{_geo['timezone']}'")  # existing, proven, 22-section
        .replace("{TIMEZONE}", _geo["timezone"])
        .replace("'{LOCALE}'", f"'{_geo['locale']}'")
        .replace("{LOCALE}", _geo["locale"])
        .replace("{CANVAS_SEED}", str(_fp.get("canvas_seed", 0x1A2B3C)))
        .replace("{AUDIO_SEED}", str(_fp.get("audio_seed", 0x4D5E6F)))
        .replace("{LAT}", str(_geo["lat"]))
        .replace("{LON}", str(_geo["lon"]))
        .replace("'{UA}'", f"'{_fp.get('ua_template', _STEALTH_UA_CHROME)}'")
        .replace("{UA}", _fp.get("ua_template", _STEALTH_UA_CHROME))
        .replace("{TZ_OFFSET}", str(_tz_offset))
        .replace("{CORES}", str(_fp.get("cores", 8)))
        .replace("{RAM}", str(_fp.get("ram", 8)))
        .replace("{PLATFORM}", _fp.get("platform", "Win32"))
        .replace("{CH_PLATFORM}", _fp.get("ch_platform", "Windows"))
        .replace("{CH_ARCH}", _fp.get("ch_arch", "x86"))
        .replace("{IS_MOBILE}", "true" if _fp.get("is_mobile") else "false")
        .replace("{SCREEN_W}", str(_fp.get("width", 1920)))
        .replace("{SCREEN_H}", _screen_h)
        .replace("{SCREEN_AH}", str(max(100, int(_screen_h) - 40)))
        .replace("{WEBGL_V}", "Google Inc. (NVIDIA)")
        .replace(
            "{WEBGL_R}",
            _fp.get(
                "webgl_renderer",
                "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ),
        )
    )

    _tmux_session = "agy_auth_flow"
    _oauth_pattern = _re_auth.compile(
        r"https://accounts\.google\.com/o/oauth2[^\s\x00-\x1f]+"
        r"|https://[^\s\x00-\x1f]+[?&]client_id=[^\s\x00-\x1f]+"
    )

    try:
        # Kill stale tmux session
        _sp.run(["tmux", "kill-session", "-t", _tmux_session], capture_output=True)
        _time.sleep(0.3)

        # Singleton locks cleaned from restored profile before any browser launch
        if _restore_dir and os.path.isdir(_restore_dir):
            for _sl in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
                try:
                    os.unlink(os.path.join(_restore_dir, _sl))
                except:
                    pass

        # Wide tmux terminal — OAuth URLs are ~700 chars; -J joins wrapped lines
        _sp.run(
            ["tmux", "new-session", "-d", "-s", _tmux_session, "-x", "500", "-y", "50"],
            check=True,
            capture_output=True,
        )

        _sp.run(
            [
                "tmux",
                "send-keys",
                "-t",
                _tmux_session,
                f"DISPLAY=:99 {_AGY_BIN} --dangerously-skip-permissions",
                "Enter",
            ],
            capture_output=True,
        )

        _start = _time.time()
        _state = "waiting_menu"
        _oauth_url = None
        _answered_prompts: set = set()

        while _time.time() - _start < _auth_timeout:
            _time.sleep(2)

            _cap = _sp.run(
                ["tmux", "capture-pane", "-t", _tmux_session, "-p", "-J"],
                capture_output=True,
                text=True,
            ).stdout
            logger.debug(f"agy tmux ({_state}): {_cap[:250]!r}")

            # ── Menu: select Google OAuth ─────────────────────────────────────
            if _state == "waiting_menu":
                if "Select login method" in _cap or "Login method" in _cap:
                    if "login_method" not in _answered_prompts:
                        _time.sleep(0.4 + _time.time() % 0.3)
                        _sp.run(
                            ["tmux", "send-keys", "-t", _tmux_session, "Enter", ""],
                            capture_output=True,
                        )
                        _answered_prompts.add("login_method")
                        _state = "waiting_url"
                        logger.info("agy_auth: selected Google OAuth (Enter)")
                if "Terms of Service" in _cap and "tos" not in _answered_prompts:
                    _sp.run(
                        ["tmux", "send-keys", "-t", _tmux_session, "y", "Enter"],
                        capture_output=True,
                    )
                    _answered_prompts.add("tos")

            # ── URL: capture OAuth URL → drive patchright browser ─────────────
            elif _state == "waiting_url":
                if "Terms of Service" in _cap and "tos" not in _answered_prompts:
                    _sp.run(
                        ["tmux", "send-keys", "-t", _tmux_session, "y", "Enter"],
                        capture_output=True,
                    )
                    _answered_prompts.add("tos")
                if (
                    "trust" in _cap.lower()
                    and "folder" in _cap.lower()
                    and "trust" not in _answered_prompts
                ):
                    _sp.run(
                        ["tmux", "send-keys", "-t", _tmux_session, "Enter", ""], capture_output=True
                    )
                    _answered_prompts.add("trust")

                _m = _oauth_pattern.search(_cap)
                if _m:
                    _oauth_url = _m.group(0).strip().rstrip(".")
                    logger.info(
                        f"agy_auth: OAuth URL ({len(_oauth_url)} chars): {_oauth_url[:80]}..."
                    )
                    _state = "driving_browser"

                    # ── Browser task: runs in a thread (sync_playwright safe outside asyncio) ──
                    # Uses EXISTING _session_stealth_js (22-section, seeded, proven),
                    # CHROMIUM_ARGS (hardened), _PATCHRIGHT_CHROME, and _load_profile_fingerprint.
                    # Proxy passed as --proxy-server= Chrome flag — matching how launch_browser()
                    # does it, NOT as patchright's proxy={} kwarg (which is a different path).
                    def _run_oauth_browser():
                        """Thread target. Returns auth code string or None."""
                        import urllib.parse as _up

                        # Prefer Chrome 131 (real browser, NOT 'Chrome for Testing').
                        # Chrome for Testing shows an "automated testing" banner which
                        # is a bot signal and causes Google to reject aicode scope.
                        _chrome_131 = "/opt/chrome131/chrome"
                        if os.path.isfile(_chrome_131):
                            _chrome = _chrome_131
                            logger.info("agy_auth/browser: using Chrome 131 (real browser)")
                        else:
                            _chrome = _PATCHRIGHT_CHROME
                            logger.info(
                                "agy_auth/browser: using patchright Chromium (Chrome 131 not found)"
                            )
                        if not _chrome:
                            logger.warning("agy_auth/browser: no Chrome binary available")
                            return None

                        # Proxy strategy: try socks5:// first (residential exit — required for
                        # anti-detection). patchright opens many internal connections through the
                        # proxy which can cause timeouts on slow WS tunnel paths. If the proxy
                        # navigation times out, fall back to direct so auth completes regardless.
                        # The profile cookies (restored from Drive) handle the consent flow —
                        # the exit IP is less critical for the one-time OAuth step than for sessions.
                        _proxy_url = "socks5://127.0.0.1:19055"  # WS bridge → PPPoE residential
                        _auth_args_proxy = list(CHROMIUM_ARGS) + [f"--proxy-server={_proxy_url}"]
                        _auth_args_direct = list(CHROMIUM_ARGS)  # no proxy — Colab direct

                        logger.info(
                            f"agy_auth/browser: launching {_chrome} "
                            f"profile={_restore_dir} proxy={_proxy_url} (direct fallback ready)"
                        )
                        try:
                            from patchright.sync_api import sync_playwright as _sync_pw

                            with _sync_pw() as _pw:
                                # Try proxy first
                                _ctx = _pw.chromium.launch_persistent_context(
                                    user_data_dir=_restore_dir,
                                    executable_path=_chrome,
                                    headless=True,
                                    user_agent=_fp.get("ua_template", _STEALTH_UA_CHROME),
                                    args=_auth_args_proxy,
                                    ignore_https_errors=True,
                                    timeout=30000,
                                )
                                _ctx.add_init_script(_session_stealth_js)
                                _pg = _ctx.new_page()

                                # Intercept OAuth URL: rewrite prompt=consent → prompt=select_account.
                                # With an active Google session in the profile, removing force-consent
                                # lets Google auto-redirect with a code instead of showing the
                                # consent form (which hits cloud-platform scope restriction).
                                def _rewrite_consent(route):
                                    req = route.request
                                    u = req.url
                                    if (
                                        "accounts.google.com/o/oauth2" in u
                                        and "prompt=consent" in u
                                    ):
                                        u2 = u.replace("prompt=consent", "prompt=select_account")
                                        logger.info(
                                            "agy_auth/browser: rewrote prompt=consent → select_account"
                                        )
                                        route.continue_(url=u2)
                                    else:
                                        route.continue_()

                                _pg.route("**/o/oauth2/**", _rewrite_consent)
                                _time.sleep(0.8 + _time.time() % 0.5)
                                _nav_ok = False
                                try:
                                    _pg.goto(
                                        _oauth_url, timeout=45000, wait_until="domcontentloaded"
                                    )
                                    _nav_ok = True
                                    logger.info("agy_auth/browser: proxy nav succeeded ✅")
                                except Exception as _ne:
                                    logger.warning(
                                        f"agy_auth/browser: proxy nav failed ({_ne}) — retrying direct"
                                    )

                                # Fallback: close proxy ctx, relaunch direct
                                if not _nav_ok:
                                    try:
                                        _ctx.close()
                                    except Exception:
                                        pass
                                    _ctx = _pw.chromium.launch_persistent_context(
                                        user_data_dir=_restore_dir,
                                        executable_path=_chrome,
                                        headless=True,
                                        user_agent=_fp.get("ua_template", _STEALTH_UA_CHROME),
                                        args=_auth_args_direct,
                                        ignore_https_errors=True,
                                        timeout=30000,
                                    )
                                    _ctx.add_init_script(_session_stealth_js)
                                    _pg = _ctx.new_page()
                                    _pg.route("**/o/oauth2/**", _rewrite_consent)
                                    _time.sleep(0.8 + _time.time() % 0.5)
                                    logger.info("agy_auth/browser: navigating direct (no proxy)")
                                    try:
                                        _pg.goto(
                                            _oauth_url, timeout=40000, wait_until="domcontentloaded"
                                        )
                                    except Exception as _ne2:
                                        logger.warning(f"agy_auth/browser: direct nav: {_ne2}")

                                _time.sleep(2)
                                logger.info(f"agy_auth/browser: at {_pg.url[:80]}")

                                # ── TOTP secret for authenticator challenges ────────────
                                import pyotp as _pyotp

                                _agy_totp_secret = os.environ.get(
                                    "XIOSYNC_TOTP_SECRET", "66pplgab2a4qzxf25tpmqs3rd4so3tuq"
                                ).replace(" ", "")

                                def _jitter_click(page, elem):
                                    """Human-like mouse move then click."""
                                    try:
                                        box = elem.bounding_box()
                                        if box:
                                            page.mouse.move(
                                                box["x"]
                                                + box["width"] / 2
                                                + (_time.time() % 2 - 1),
                                                box["y"]
                                                + box["height"] / 2
                                                + (_time.time() % 1.5 - 0.75),
                                            )
                                            _time.sleep(0.15 + _time.time() % 0.15)
                                        elem.click()
                                        return True
                                    except Exception:
                                        return False

                                def _first_visible(page, selectors):
                                    """Return first visible element matching any selector."""
                                    for sel in selectors:
                                        try:
                                            el = page.query_selector(sel)
                                            if el and el.is_visible():
                                                return el
                                        except Exception:
                                            pass
                                    return None

                                _last_handled = ""
                                for _wi in range(30):  # up to 90s (3s × 30)
                                    _cur = _pg.url
                                    _time.sleep(0.3)

                                    # ── 1. Callback landed ──────────────────────────────
                                    if "antigravity.google" in _cur or "oauth-callback" in _cur:
                                        logger.info(f"agy_auth/browser: ✅ [callback] {_cur[:120]}")
                                        break

                                    # ── 2. Account chooser ──────────────────────────────
                                    if "accountchooser" in _cur or (
                                        "v3/signin" in _cur
                                        and "accountchooser" not in _last_handled
                                    ):
                                        logger.info(
                                            f"agy_auth/browser: [account_chooser] iter={_wi}"
                                        )
                                        _last_handled = "accountchooser"
                                        _time.sleep(1.5)
                                        # Try clicking the target email account
                                        _clicked_acct = False
                                        try:
                                            _acct_js = """
                                            (function() {
                                                var candidates = document.querySelectorAll(
                                                    '[data-email],[data-identifier],li[data-authuser],.OVnw0d,[jsname="rymPhb"]'
                                                );
                                                for (var el of candidates) {
                                                    var em = el.getAttribute('data-email') || el.getAttribute('data-identifier') || el.textContent || '';
                                                    if (em.includes('gmail') || em.includes('@')) {
                                                        el.click(); return 'clicked:' + em.slice(0,40);
                                                    }
                                                }
                                                // Fallback: first focusable account tile
                                                var tiles = document.querySelectorAll('[tabindex="0"]');
                                                for (var t of tiles) {
                                                    if (t.textContent.includes('@gmail') || t.textContent.includes('@google')) {
                                                        t.click(); return 'clicked_tile:' + t.textContent.slice(0,30);
                                                    }
                                                }
                                                // Last resort: first li/div in the chooser
                                                var first = document.querySelector('#initialView li, .OVnw0d, [jsname="rymPhb"]');
                                                if (first) { first.click(); return 'clicked_first'; }
                                                return 'not_found';
                                            })()
                                            """
                                            _acct_res = _pg.evaluate(_acct_js)
                                            logger.info(
                                                f"agy_auth/browser: [account_chooser] JS result: {_acct_res}"
                                            )
                                            _clicked_acct = True
                                        except Exception as _ae:
                                            logger.warning(
                                                f"agy_auth/browser: [account_chooser] JS error: {_ae}"
                                            )
                                        if not _clicked_acct:
                                            _el = _first_visible(
                                                _pg,
                                                [
                                                    "li[data-authuser]",
                                                    ".OVnw0d",
                                                    "[jsname='rymPhb']",
                                                    "[data-email]",
                                                    "[data-identifier]",
                                                ],
                                            )
                                            if _el:
                                                _jitter_click(_pg, _el)
                                                logger.info(
                                                    "agy_auth/browser: [account_chooser] clicked element"
                                                )
                                        _time.sleep(3)
                                        continue

                                    # ── 3. "Make sure it's you" / first-party confirm ───
                                    if (
                                        "nativeapp" in _cur
                                        or "firstparty" in _cur
                                        or ("oauth/v3" in _cur and "accountchooser" not in _cur)
                                    ):
                                        logger.info(
                                            f"agy_auth/browser: [make_sure] iter={_wi} url={_cur[:80]}"
                                        )
                                        _last_handled = "make_sure"
                                        _time.sleep(1.5)
                                        # Try confirm/continue button
                                        _confirm = _first_visible(
                                            _pg,
                                            [
                                                "button:has-text('Continue')",
                                                "button:has-text('Yes')",
                                                "button:has-text('Next')",
                                                "button:has-text('Confirm')",
                                                'button:has-text("Yes, it\'s me")',
                                                "#next",
                                                "[jsname='LgbsSe']",
                                                "button[type='submit']",
                                            ],
                                        )
                                        if _confirm:
                                            _jitter_click(_pg, _confirm)
                                            logger.info(
                                                "agy_auth/browser: [make_sure] clicked confirm button"
                                            )
                                            _time.sleep(3)
                                        else:
                                            logger.warning(
                                                "agy_auth/browser: [make_sure] no confirm button found"
                                            )
                                        continue

                                    # ── 4. Google Authenticator TOTP challenge ──────────
                                    if "/challenge/totp" in _cur or "/challenge/ipp" in _cur:
                                        logger.info(f"agy_auth/browser: [totp] iter={_wi}")
                                        _last_handled = "totp"
                                        _time.sleep(1.0)
                                        # Wait for fresh TOTP window (>5s remaining)
                                        _totp_remaining = 30 - (_time.time() % 30)
                                        if _totp_remaining < 5:
                                            logger.info(
                                                f"agy_auth/browser: [totp] waiting {_totp_remaining:.1f}s for fresh window"
                                            )
                                            _time.sleep(_totp_remaining + 0.5)
                                        _totp_code = _pyotp.TOTP(_agy_totp_secret).now()
                                        logger.info(
                                            f"agy_auth/browser: [totp] code=****{_totp_code[-2:]}"
                                        )
                                        # Find the input field
                                        _totp_input = _first_visible(
                                            _pg,
                                            [
                                                "input[type='tel']",
                                                "input[type='number']",
                                                "input[aria-label*='code' i]",
                                                "input[aria-label*='Code' i]",
                                                "input[autocomplete='one-time-code']",
                                                "input[id*='totp']",
                                                "input[id*='code']",
                                                "input[name='totpPin']",
                                                "input[name='code']",
                                                "#totpPin",
                                                "#code",
                                            ],
                                        )
                                        if _totp_input:
                                            _totp_input.click()
                                            _time.sleep(0.3)
                                            _totp_input.fill("")
                                            _time.sleep(0.2)
                                            _totp_input.type(_totp_code, delay=80)
                                            _time.sleep(0.5)
                                            _next = _first_visible(
                                                _pg,
                                                [
                                                    "#next",
                                                    "button[type='submit']",
                                                    "button:has-text('Next')",
                                                    "button:has-text('Verify')",
                                                    "[jsname='LgbsSe']",
                                                ],
                                            )
                                            if _next:
                                                _jitter_click(_pg, _next)
                                                logger.info(
                                                    "agy_auth/browser: [totp] submitted code"
                                                )
                                            _time.sleep(4)
                                        else:
                                            logger.warning(
                                                "agy_auth/browser: [totp] no input found"
                                            )
                                        continue

                                    # ── 5. agy device/display_code page ────────────────
                                    if "/device" in _cur or "device_code" in _cur:
                                        logger.info(f"agy_auth/browser: [display_code] iter={_wi}")
                                        _last_handled = "display_code"
                                        _time.sleep(1.5)
                                        # Capture display_code from the tmux agy session
                                        try:
                                            _pane_out = _sp.check_output(
                                                ["tmux", "capture-pane", "-p", "-t", _tmux_session],
                                                text=True,
                                                timeout=5,
                                            )
                                            # agy displays a short code like "ABC-123" or "ABCD1234"
                                            _dc_match = _re_auth.search(
                                                r"\b([A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8})?)\b", _pane_out
                                            )
                                            _display_code = _dc_match.group(1) if _dc_match else ""
                                        except Exception:
                                            _display_code = ""
                                        if _display_code:
                                            logger.info(
                                                f"agy_auth/browser: [display_code] code={_display_code}"
                                            )
                                            _dc_input = _first_visible(
                                                _pg,
                                                [
                                                    "input[id*='code']",
                                                    "input[name*='code']",
                                                    "input[aria-label*='code' i]",
                                                    "input[type='text']",
                                                ],
                                            )
                                            if _dc_input:
                                                _dc_input.fill(_display_code)
                                                _time.sleep(0.4)
                                                _ver = _first_visible(
                                                    _pg,
                                                    [
                                                        "button:has-text('Verify')",
                                                        "button:has-text('Next')",
                                                        "button[type='submit']",
                                                        "#submit",
                                                    ],
                                                )
                                                if _ver:
                                                    _jitter_click(_pg, _ver)
                                                    logger.info(
                                                        "agy_auth/browser: [display_code] submitted"
                                                    )
                                                _time.sleep(3)
                                        else:
                                            logger.warning(
                                                "agy_auth/browser: [display_code] no code found in tmux pane"
                                            )
                                        continue

                                    # ── 6. Allow / consent screen ───────────────────────
                                    if "accounts.google.com" in _cur:
                                        logger.info(
                                            f"agy_auth/browser: [consent] iter={_wi} url={_cur[:80]}"
                                        )
                                        _last_handled = "consent"
                                        _time.sleep(1.2 + _time.time() % 0.8)
                                        _allow = _first_visible(
                                            _pg,
                                            [
                                                "button[jsname='LgbsSe']",
                                                "button:has-text('Allow')",
                                                "button:has-text('Continue')",
                                                "button:has-text('Yes, I\\'m in')",
                                                "#submit_approve_access",
                                                "[data-action='allow']",
                                                "div[role='button']:has-text('Allow')",
                                            ],
                                        )
                                        if _allow:
                                            _jitter_click(_pg, _allow)
                                            logger.info(
                                                "agy_auth/browser: [consent] clicked Allow/Continue"
                                            )
                                            _time.sleep(3)
                                        else:
                                            logger.info(
                                                "agy_auth/browser: [consent] no button yet, waiting..."
                                            )
                                        continue

                                    # Unknown page — wait and retry
                                    logger.info(
                                        f"agy_auth/browser: [waiting] iter={_wi} url={_cur[:80]}"
                                    )
                                    _time.sleep(3)

                                _final = _pg.url
                                _qp = dict(_up.parse_qsl(_up.urlparse(_final).query))
                                _code = _qp.get("code", "")
                                if not _code:
                                    _frag = dict(_up.parse_qsl(_up.urlparse(_final).fragment))
                                    _code = _frag.get("code", "")
                                if not _code:
                                    try:
                                        _bt = _pg.inner_text("body")
                                        _cm2 = _re_auth.search(
                                            r"code[=:\s]+([A-Za-z0-9/_\-\.]{20,})", _bt
                                        )
                                        if _cm2:
                                            _code = _cm2.group(1)
                                    except Exception:
                                        pass

                                logger.info(
                                    f"agy_auth/browser: code={'OBTAINED' if _code else 'NONE'}"
                                )
                                _ctx.close()
                                return _code
                        except Exception as _be:
                            logger.error(f"agy_auth/browser: error: {_be}")
                            return None

                    # Run in thread — keeps the asyncio event loop free
                    _auth_code = None
                    try:
                        from concurrent.futures import ThreadPoolExecutor as _TPE

                        with _TPE(max_workers=1) as _ex:
                            _auth_code = _ex.submit(_run_oauth_browser).result(timeout=180)
                    except Exception as _te:
                        logger.error(f"agy_auth: thread executor error: {_te}")

                    if _auth_code:
                        _time.sleep(0.5)
                        _sp.run(
                            ["tmux", "send-keys", "-t", _tmux_session, _auth_code, "Enter"],
                            capture_output=True,
                        )
                        logger.info("agy_auth: ✅ sent auth code to agy")
                        _state = "waiting_confirm"
                    else:
                        logger.warning("agy_auth: no auth code — agy will wait at manual prompt")
                        _state = "waiting_confirm"

            # ── Confirm: agy shows signed-in screen ──────────────────────────
            elif _state in ("waiting_confirm", "driving_browser"):
                _success_markers = [
                    "signed in",
                    "Signed in",
                    "logged in",
                    "Logged in",
                    "Welcome",
                    "Authentication successful",
                    "Successfully authenticated",
                    "@gmail.com",  # any Google account confirmation
                ]
                if any(_mk in _cap for _mk in _success_markers):
                    result["auth_success"] = True
                    logger.info("agy_auth: ✅ authentication confirmed in TUI")
                    break

            # Session exit detection
            if (
                _sp.run(["tmux", "list-panes", "-t", _tmux_session], capture_output=True).returncode
                != 0
            ):
                if _is_agy_authenticated():
                    result["auth_success"] = True
                    logger.info("agy_auth: session exited + creds found → success")
                break

        _final_cap = _sp.run(
            ["tmux", "capture-pane", "-t", _tmux_session, "-p", "-J"],
            capture_output=True,
            text=True,
        ).stdout
        logger.info(f"agy_auth final screen: {_final_cap[:300]!r}")

    except Exception as auth_exc:
        result["error"] = f"agy auth error: {auth_exc}"
        logger.error(f"ai_install_agy: auth error: {auth_exc}")
    finally:
        _sp.run(["tmux", "kill-session", "-t", _tmux_session], capture_output=True)

    # ── Step 5: Persist agy credentials to Drive ────────────────────────────────
    # On Linux, agy stores tokens in ~/.gemini/ (no Keychain) — portable files.
    _agy_creds_src = os.path.expanduser("~/.gemini")
    _agy_creds_dst = os.path.join(DRIVE_ROOT, "cache/agy-credentials.tar.gz")
    # Persist any auth-related files (exclude heavy dirs like antigravity/brain)
    _creds_dirs = ["antigravity-cli", "google_accounts.json", "state.json", "installation_id"]

    if result["auth_success"]:
        try:
            import tarfile as _tf_cred

            os.makedirs(os.path.dirname(_agy_creds_dst), exist_ok=True)
            with _tf_cred.open(_agy_creds_dst + ".tmp", "w:gz") as tf:
                for _item in _creds_dirs:
                    _full = os.path.join(_agy_creds_src, _item)
                    if os.path.exists(_full):
                        # arcname=".gemini/{item}" + extract to HOME (/root)
                        # → /root/.gemini/{item} — correct final path.
                        # Do NOT extract to ~/.gemini or it becomes ~/.gemini/.gemini/{item}
                        tf.add(_full, arcname=os.path.join(".gemini", _item))
            _sh.move(_agy_creds_dst + ".tmp", _agy_creds_dst)
            result["creds_persisted"] = True
            logger.info(f"ai_install_agy: credentials persisted to Drive {_agy_creds_dst}")
            # Also save account-bound copy: agy-credentials-{email_slug}.tar.gz
            try:
                import base64 as _b64cred
                import json as _jcred

                _tok_fp = os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token")
                if os.path.isfile(_tok_fp):
                    _tok_d = _jcred.loads(open(_tok_fp).read())
                    _id_tok = _tok_d.get("id_token", "")
                    _parts = _id_tok.split(".")
                    if len(_parts) >= 2:
                        _payload = _jcred.loads(_b64cred.b64decode(_parts[1] + "===").decode())
                        _email = _payload.get("email", "")
                        if _email:
                            _slug = _email.replace("@", "_at_").replace(".", "_")
                            _acct_dst = os.path.join(
                                DRIVE_ROOT, f"cache/agy-credentials-{_slug}.tar.gz"
                            )
                            import shutil as _shcred

                            _shcred.copy2(_agy_creds_dst, _acct_dst)
                            result["creds_account_key"] = _slug
                            logger.info(
                                f"ai_install_agy: account-bound backup saved → agy-credentials-{_slug}.tar.gz"
                            )
            except Exception as _acct_err:
                logger.warning(f"ai_install_agy: account-bound copy failed: {_acct_err}")
        except Exception as cp_err:
            logger.warning(f"ai_install_agy: creds persist failed: {cp_err}")

    return result


async def ai_restore_agy_creds() -> dict:
    """Restore persisted agy credentials from Drive (for runtime reboot recovery).

    On a fresh Colab runtime, call this before /ai/generate to avoid re-auth.
    Restores ~/.gemini/antigravity-cli/ from Drive cache/agy-credentials.tar.gz.
    """
    import shutil as _sh
    import tarfile as _tf_rc

    _src = os.path.join(DRIVE_ROOT, "cache/agy-credentials.tar.gz")
    _dst = os.path.expanduser("~/.gemini")

    if not os.path.isfile(_src):
        return {
            "ok": False,
            "reason": "No persisted credentials at Drive cache/agy-credentials.tar.gz",
        }

    try:
        # Extract to HOME (/root), NOT to ~/.gemini.
        # tar entries are arcname=".gemini/{item}", so extracting to HOME
        # produces /root/.gemini/{item}. Extracting to ~/.gemini would produce
        # /root/.gemini/.gemini/{item} — the double-nesting bug.
        _home = os.path.expanduser("~")
        os.makedirs(os.path.join(_home, ".gemini"), exist_ok=True)
        _agy_dir = os.path.join(_home, ".gemini", "antigravity-cli")
        if os.path.isdir(_agy_dir):
            _sh.rmtree(_agy_dir)
        with _tf_rc.open(_src, "r:gz") as tf:
            tf.extractall(path=_home)
        logger.info(f"ai_restore_agy_creds: restored to {_agy_dir}")
        return {"ok": True, "restored_to": _agy_dir, "authenticated": _is_agy_authenticated()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


