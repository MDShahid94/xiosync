#!/usr/bin/env python3
"""
XIO Mesh — Google Stealth Login Sidecar
========================================
Multi-engine login runner with adaptive fallback:
  1. camoufox  — Firefox C++-level spoofing (theoretically strongest)
  2. uc        — undetected-chromedriver (PROVEN to work — reference notebook)
  3. nodriver  — modern uc successor, pure CDP no WebDriver

The calling workflow (google-signin.mjs) chooses the engine order
based on the UCB1 Engine Success Registry. Engines are passed via --engines arg.

Usage:
  python3 google_stealth_login.py \\
    --email user@gmail.com \\
    --password "pass" \\
    --totp_secret "BASE32SECRET" \\
    --socks5 socks5://127.0.0.1:1055 \\
    --output /content/xio-mesh/sessions/session_id.json \\
    --engines camoufox,uc,nodriver \\
    --screenshot_dir /content/xio-mesh/jobs \\
    --workflow_id google-signin \\
    --domain accounts.google.com

Exit codes:
  0 = success (session JSON written)
  1 = all engines failed
  2 = bot-detection redirect
"""

import argparse, asyncio, json, sys, os, time, struct, hmac, hashlib, base64
from pathlib import Path

DISPLAY = os.environ.get('DISPLAY', ':99')

# ── TOTP Generator ────────────────────────────────────────────────────────────

def base32_decode(encoded: str) -> bytes:
    s = encoded.upper().replace(' ', '')
    pad = (8 - len(s) % 8) % 8
    return base64.b32decode(s + '=' * pad)

def generate_totp(secret: str) -> str:
    key = base32_decode(secret)
    counter = int(time.time()) // 30
    msg = struct.pack('>Q', counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = (struct.unpack('>I', h[offset:offset+4])[0] & 0x7FFFFFFF) % 1_000_000
    return str(code).zfill(6)

# ── Shared Utilities ──────────────────────────────────────────────────────────

def log(msg: str):
    print(msg, flush=True)

def save_screenshot(page_or_driver, path: str, engine: str):
    try:
        if engine in ('camoufox', 'nodriver_pw'):
            # Playwright-compatible
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                page_or_driver.screenshot(path=path)
            ) if not asyncio.iscoroutine(page_or_driver.screenshot(path=path)) else None
        elif engine == 'uc':
            uc_snap(page_or_driver, path)
    except Exception as e:
        log(f"  ⚠️  Screenshot failed: {e}")

def uc_snap(driver, path: str, quality: int = 70):
    """Save a Selenium screenshot as JPEG (via PIL) — avoids PNG extension warning.
    Selenium's save_screenshot() always produces PNG regardless of extension.
    This helper captures raw PNG bytes and converts to JPEG in-memory."""
    try:
        import io
        from PIL import Image
        png_bytes = driver.get_screenshot_as_png()
        img = Image.open(io.BytesIO(png_bytes)).convert('RGB')
        # Ensure .jpg extension
        out_path = path if path.endswith('.jpg') else path.rsplit('.', 1)[0] + '.jpg'
        img.save(out_path, 'JPEG', quality=quality)
    except Exception as e:
        log(f"  ⚠️  uc_snap failed ({path}): {e}")
        # Fallback to native save (will be PNG with wrong extension warning)
        try: driver.save_screenshot(path)
        except: pass

def to_playwright_cookies(raw_cookies: list) -> list:
    """Normalize cookie dicts to Playwright storageState format."""
    result = []
    for c in raw_cookies:
        result.append({
            "name":     c.get("name", ""),
            "value":    c.get("value", ""),
            "domain":   c.get("domain", ""),
            "path":     c.get("path", "/"),
            "expires":  c.get("expires", -1),
            "httpOnly": c.get("httpOnly", False),
            "secure":   c.get("secure", False),
            "sameSite": c.get("sameSite", "Lax"),
        })
    return result

def write_session(cookies: list, output_path: str):
    session_data = {
        "cookies": to_playwright_cookies(cookies),
        "origins": []
    }
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(json.dumps(session_data, indent=2))
    log(f"[stealth-login] ✅ Session written to {output_path}")

# ══════════════════════════════════════════════════════════════════════════════
# ENGINE 1: camoufox (Firefox, C++ level spoofing)
# ══════════════════════════════════════════════════════════════════════════════

async def run_camoufox(email, password, totp_secret, socks5, output_path, screenshot_dir):
    log("[camoufox] Starting Firefox stealth session...")
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        log("[camoufox] Not installed — skipping")
        return False

    proxy = {"server": socks5} if socks5 else None

    try:
        async with AsyncCamoufox(
            headless=True,
            os="macos",
            screen={"width": 1920, "height": 1080},
            proxy=proxy,
            humanize=True,
            geoip=True,
        ) as browser:
            ctx = await browser.new_context(
                locale="en-US",
                timezone_id="America/New_York",
                viewport={"width": 1440, "height": 900},
            )
            page = await ctx.new_page()

            # Phase 0: Check existing session
            await page.goto("https://myaccount.google.com/", wait_until="domcontentloaded")
            await asyncio.sleep(1.5)
            if "myaccount.google.com" in page.url and "signin" not in page.url:
                log("[camoufox] ✅ Already authenticated")
            else:
                # Warmup
                for url in ["https://www.google.com", "https://news.google.com"]:
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                        await asyncio.sleep(1.2 + __import__('random').random())
                    except Exception: pass

                # Sign-in
                await page.goto(
                    "https://accounts.google.com/ServiceLogin?service=mail&hl=en"
                    "&continue=https://mail.google.com",
                    wait_until="domcontentloaded", timeout=25000
                )
                await asyncio.sleep(2.5)

                # Email
                email_input = page.locator("input[type='email'], input[name='identifier']")
                await email_input.wait_for(state="visible", timeout=10000)
                await email_input.click()
                await asyncio.sleep(0.5)
                for ch in email:
                    await email_input.type(ch, delay=75 + int(55 * __import__('random').random()))
                await asyncio.sleep(0.8)
                await page.locator("#identifierNext, button[type='submit']").first.click()
                await asyncio.sleep(3.5)

                cur_url = page.url
                log(f"[camoufox] Post-email URL: {cur_url[:100]}")
                if "rejected" in cur_url:
                    await page.screenshot(path=f"{screenshot_dir}/camoufox_rejected.png")
                    log("[camoufox] ❌ Bot-detection redirect")
                    return False

                # Password
                try:
                    pw = page.locator("input[type='password']").first
                    await pw.wait_for(state="visible", timeout=10000)
                    await pw.click()
                    await asyncio.sleep(0.5)
                    for ch in password:
                        await pw.type(ch, delay=70 + int(50 * __import__('random').random()))
                    await asyncio.sleep(0.7)
                    await page.locator("#passwordNext, button[type='submit']").first.click()
                    await asyncio.sleep(4)
                except Exception as e:
                    log(f"[camoufox] ⚠️ Password field: {e}")

                # TOTP
                body = await page.evaluate("document.body.innerText")
                if totp_secret and any(k in body for k in ["2-Step", "authenticator", "verification code"]):
                    code = generate_totp(totp_secret)
                    log(f"[camoufox] 2FA detected, entering TOTP...")
                    try:
                        totp_in = page.locator("input[type='tel'], input[type='number'], input[name='totpPin']").first
                        await totp_in.wait_for(state="visible", timeout=8000)
                        await totp_in.fill(code)
                        await asyncio.sleep(0.6)
                        await page.locator("#totpNext, button[type='submit']").first.click()
                        await asyncio.sleep(4)
                    except Exception as e:
                        log(f"[camoufox] ⚠️ TOTP field: {e}")

            # Verify
            await page.goto("https://myaccount.google.com/", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            await page.screenshot(path=f"{screenshot_dir}/camoufox_final.png")
            if "myaccount.google.com" in page.url and "Sign in" not in await page.title():
                cookies = await ctx.cookies()
                write_session(cookies, output_path)
                return True
            log("[camoufox] ❌ Verification failed")
            return False
    except Exception as e:
        log(f"[camoufox] ❌ Exception: {e}")
        return False

# ══════════════════════════════════════════════════════════════════════════════
# ENGINE 2: undetected-chromedriver (uc) — PROVEN in reference notebook
# Borrows exact flags + fingerprint from Colab_Antibot_Browser.ipynb
# ══════════════════════════════════════════════════════════════════════════════


# reCAPTCHA audio solver — isolated, self-installing, reusable by any workflow.
# All solving logic lives in colab/recaptcha_solver.py.
import importlib.util as _ilib, os as _os
_rc_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'recaptcha_solver.py')
_rc_spec = _ilib.spec_from_file_location('recaptcha_solver', _rc_path)
_rc_mod  = _ilib.module_from_spec(_rc_spec)
_rc_spec.loader.exec_module(_rc_mod)


def handle_recaptcha_buster(driver, log_fn, sleep_fn=None):
    """
    Thin wrapper → delegates to recaptcha_solver.solve_recaptcha.
    Drop-in compatible: all existing call sites remain unchanged.
    """
    return _rc_mod.solve_recaptcha(driver, log_fn=log_fn, sleep_fn=sleep_fn, max_attempts=2)



def run_uc(email, password, totp_secret, socks5, output_path, screenshot_dir, profile_dir=None, signal_file="/tmp/xio_uc_2fa_signal.json", resume_file="/tmp/xio_uc_2fa_resume.json"):
    log("[uc] Starting undetected-chromedriver session (proven reference)...")
    try:
        import undetected_chromedriver as uc_mod
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        import subprocess
    except ImportError:
        log("[uc] Installing undetected-chromedriver...")
        os.system("pip install undetected-chromedriver selenium -q")
        try:
            import undetected_chromedriver as uc_mod
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
        except ImportError:
            log("[uc] ❌ Install failed — skipping")
            return False

    import random

    try:
        # Get Chrome version
        try:
            cv_str = subprocess.check_output(
                ['google-chrome', '--version'], stderr=subprocess.DEVNULL
            ).decode().strip().split()[-1]
            cv_major = int(cv_str.split('.')[0])
        except Exception:
            cv_major = 131

        # Exact options from Colab_Antibot_Browser.ipynb reference notebook
        options = uc_mod.ChromeOptions()
        if socks5:
            proxy_addr = socks5.replace('socks5://', '')
            options.add_argument(f'--proxy-server=socks5://{proxy_addr}')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-setuid-sandbox')  # belt+braces for Colab root
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--use-gl=angle')
        options.add_argument('--use-angle=swiftshader')
        options.add_argument('--disable-gpu-sandbox')
        options.add_argument('--ignore-gpu-blocklist')
        options.add_argument('--disable-service-workers')
        options.add_argument('--disable-features=ServiceWorker,UserAgentClientHint')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--window-position=0,0')
        options.add_argument(f'--display={DISPLAY}')
        # Expose remote debugging so DevTools MCP can take over at 2FA
        import socket
        s = socket.socket()
        s.bind(('', 0))
        UC_DEBUG_PORT = s.getsockname()[1]
        s.close()
        options.add_argument(f'--remote-debugging-port={UC_DEBUG_PORT}')
        options.add_argument('--remote-debugging-address=0.0.0.0')

        options.add_experimental_option("prefs", {
            "webrtc.ip_handling_policy": "disable_non_proxied_udp",
            "webrtc.multiple_routes_enabled": False,
            "webrtc.nonproxied_udp_enabled": False,
        })

        # Persist the browser profile so a signed-in Chrome profile is produced
        if profile_dir:
            os.makedirs(profile_dir, exist_ok=True)
            options.add_argument(f'--user-data-dir={profile_dir}')
            log(f"[uc] 💾 Profile dir: {profile_dir}")

        log(f"[uc] Launching Chrome (version_main={cv_major}, DISPLAY={DISPLAY}, port={UC_DEBUG_PORT})")

        # Pre-flight SOCKS5 proxy check — ERR_PROXY_CONNECTION_FAILED happens when
        # Chrome launches before Tailscale userspace networking has established the
        # exit-node route. TCP port may be open but SOCKS5 tunnel not yet ready.
        # After 3 failures (15s), actively trigger tailscale re-up to accelerate recovery.
        if socks5:
            import subprocess as _subp
            _proxy_ready = False
            _check_url = 'https://www.google.com'
            _ts_restart_done = False
            for _attempt in range(24):  # try up to 120s (24 × 5s)
                try:
                    _rc = _subp.run(
                        ['curl', '-s', '--max-time', '5',
                         '--proxy', socks5,
                         '-o', '/dev/null', '-w', '%{http_code}',
                         _check_url],
                        capture_output=True, text=True, timeout=8
                    )
                    _code = _rc.stdout.strip()
                    if _code and _code != '000':
                        _proxy_ready = True
                        if _attempt > 0:
                            log(f"[uc] ✅ SOCKS5 proxy ready after {_attempt * 5}s (HTTP {_code})")
                        break
                    raise OSError(f"HTTP {_code}")
                except Exception as _pe:
                    log(f"[uc] ⏳ SOCKS5 not ready (attempt {_attempt+1}/24): {_pe} — waiting 5s...")
                    # After 3 failures, nudge tailscale to re-establish the exit-node route
                    if _attempt == 2 and not _ts_restart_done:
                        log("[uc] 🔄 Triggering tailscale re-up to accelerate exit-node reconnect...")
                        try:
                            _subp.run(
                                ['sudo', 'tailscale', 'up', '--accept-routes', '--ssh', '--reset',
                                 '--hostname=colab-master', '--exit-node=100.86.149.127'],
                                capture_output=True, timeout=15
                            )
                        except Exception as _tse:
                            log(f"[uc] ⚠️  tailscale re-up failed: {_tse}")
                        _ts_restart_done = True
                    time.sleep(5)
            if not _proxy_ready:
                log("[uc] ❌ SOCKS5 proxy unreachable after 120s — aborting")
                return False

        driver = uc_mod.Chrome(options=options, headless=False, version_main=cv_major, port=UC_DEBUG_PORT)

        wait = WebDriverWait(driver, 15)

        def uc_sleep(a=0.8, b=2.0):
            time.sleep(a + random.random() * (b - a))

        def uc_type(element, text):
            for ch in text:
                element.send_keys(ch)
                time.sleep(0.07 + random.random() * 0.08)

        try:
            # Phase 0: Check existing
            driver.get("https://myaccount.google.com/")
            uc_sleep(1.5, 2.5)
            if "myaccount.google.com" in driver.current_url and "signin" not in driver.current_url:
                log("[uc] ✅ Already authenticated")
            else:
                # Warmup
                for url in ["https://www.google.com", "https://news.google.com"]:
                    try:
                        driver.get(url)
                        uc_sleep(1.0, 2.0)
                    except Exception: pass

                # Sign-in page
                driver.get(
                    "https://accounts.google.com/ServiceLogin?service=mail"
                    "&hl=en&continue=https://mail.google.com"
                )
                uc_sleep(2.5, 4.0)

                # Email
                try:
                    email_el = wait.until(EC.visibility_of_element_located(
                        (By.CSS_SELECTOR, "input[type='email'], input[name='identifier']")
                    ))
                    email_el.click()
                    uc_sleep(0.4, 0.8)
                    uc_type(email_el, email)
                    uc_sleep(0.6, 1.2)
                    next_btn = driver.find_element(By.ID, "identifierNext")
                    next_btn.click()
                    uc_sleep(3.0, 5.0)
                    uc_snap(driver, f"{screenshot_dir}/uc_email_submitted.jpg")
                    log(f"[uc] 📸 uc_email_submitted.jpg")
                except Exception as e:
                    log(f"[uc] ⚠️ Email phase: {e}")

                cur_url = driver.current_url
                log(f"[uc] Post-email URL: {cur_url[:100]}")
                if "rejected" in cur_url:
                    uc_snap(driver, f"{screenshot_dir}/uc_rejected.jpg")
                    log("[uc] ❌ Bot-detection redirect")
                    driver.quit()
                    return False

                # Password
                try:
                    pw_el = wait.until(EC.visibility_of_element_located(
                        (By.CSS_SELECTOR, "input[type='password']")
                    ))

                    _cur_url_pw = driver.current_url
                    _on_challenge_pwd = 'challenge/pwd' in _cur_url_pw

                    # ── Check if field already has a value (autocomplete/autofill) ──────
                    # Chrome's autofill may pre-populate the field. If so, skip typing.
                    _pre_val = pw_el.get_attribute('value') or ''
                    log(f"[uc] 🔍 Password field pre-value: len={len(_pre_val)} (autocomplete={'YES' if _pre_val else 'NO'})")

                    if _pre_val.strip():
                        log("[uc] ✅ Field already has value — skipping typing, clicking Next")
                    elif _on_challenge_pwd:
                        # ── Google v3/signin/challenge/pwd: React-controlled input ────────
                        # CDP Input.insertText generates isTrusted:true browser-level events,
                        # bypassing UC's WebDriver protocol which marks events as not trusted.
                        # _valueTracker reset ensures React sees the value as a new change.
                        log("[uc] 🔑 challenge/pwd — CDP Input.insertText (isTrusted events)")
                        # Focus + reset React tracker
                        driver.execute_script("""
var el = arguments[0];
el.focus();
if (el._valueTracker) { el._valueTracker.setValue(''); }
""", pw_el)
                        uc_sleep(0.3, 0.5)
                        # CDP insertText — real isTrusted keystroke events
                        driver.execute_cdp_cmd("Input.insertText", {"text": password})
                        uc_sleep(0.4, 0.6)
                        # Flush React synthetic event chain
                        _post_val = driver.execute_script("""
var el = arguments[0];
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
return el.value;
""", pw_el)
                        log(f"[uc] 🔑 After CDP insertText: '{str(_post_val)[:3]}...' len={len(_post_val or '')}")
                        # Diagnostic snapshot BEFORE submit — confirms field visual state
                        uc_snap(driver, f"{screenshot_dir}/uc_pw_before_submit.jpg")
                        log(f"[uc] 📸 uc_pw_before_submit.jpg (check if bullets visible)")
                    else:
                        # Classic password page — uc_type with send_keys
                        pw_el.click()
                        uc_sleep(0.4, 0.8)
                        uc_type(pw_el, password)
                        _post_val = pw_el.get_attribute('value') or ''
                        log(f"[uc] 🔑 After send_keys: len={len(_post_val)}")

                    # ── Submit ───────────────────────────────────────────────────────────
                    uc_sleep(0.5, 1.0)
                    _btn_clicked = driver.execute_script("""
var btn = document.getElementById('passwordNext')
    || document.querySelector('[data-action=passwordNext]')
    || document.querySelector('button[type=submit]');
if (btn) { btn.click(); return 'clicked:' + (btn.id || btn.type); }
return 'not_found';
""")
                    log(f"[uc] 🔑 Submit button: {_btn_clicked}")
                    if _btn_clicked == 'not_found':
                        from selenium.webdriver.common.keys import Keys
                        pw_el.send_keys(Keys.RETURN)

                    uc_sleep(3.5, 5.0)
                    uc_snap(driver, f"{screenshot_dir}/uc_password_submitted.jpg")
                    log(f"[uc] 📸 uc_password_submitted.jpg")
                    _url_after_pw = driver.current_url
                    if 'challenge/pwd' in _url_after_pw:
                        log(f"[uc] ❌ Still on challenge/pwd after submit — password not accepted")
                        try: driver.quit()
                        except Exception: pass
                        return False
                except Exception as e:
                    log(f"[uc] ⚠️ Password phase: {e}")







                # ── 2FA / Challenge handling ────────────────────────────────────────
                # Ported from XIO_VERSE 2 auth_manager.py:
                #   - JS multi-event dispatch (pointerdown+click) avoids Selenium crash
                #   - getBoundingClientRect() for visibility-aware TOTP input detection
                #   - data-challengetype="6" for direct Authenticator nav
                #   - Post-login skip/cancel prompt handler
                try:
                    body_text = driver.find_element(By.TAG_NAME, "body").text
                except Exception:
                    body_text = ""

                cur_url_after_pw = driver.current_url
                # 'challenge/pwd' = password step itself (not 2FA) — exclude from 2FA routing.
                # Real 2FA URLs contain: selection, totp, 2sv, lookup (not /pwd).
                # Device push/notification challenge URLs: ipp, dp, az, sk, iap, iph (Google rotates these).
                _is_pwd_challenge = 'challenge/pwd' in cur_url_after_pw
                is_challenge = (not _is_pwd_challenge) and (
                    any(k in cur_url_after_pw for k in [
                        'signin/v2/challenge', 'signin/challenge',
                        '2sv', 'totp', 'lookup', 'selection',
                        'challenge/ipp', 'challenge/dp', 'challenge/az',
                        'challenge/sk', 'challenge/iap', 'challenge/iph',
                        'challenge/sl', 'challenge/dk',
                    ]) or any(k in body_text for k in [
                        '2-Step', 'authenticator', 'verification code',
                        'Check your', 'Try another way', 'Verification',
                        '2-step verification', 'sent a notification',
                    ])
                )
                if _is_pwd_challenge and not is_challenge:
                    log(f"[uc] ⚠️ Still on challenge/pwd after password submit — password may not have registered")


                if is_challenge:
                    log(f"[uc] 🔐 2FA/Challenge at: {cur_url_after_pw[:80]}")
                    # Note: uc_password_submitted.jpg already captured this screen — no duplicate shot here

                    # Write CDP signal file for monitoring (non-blocking)
                    import json as _json
                    signal_path = signal_file
                    with open(signal_path, 'w') as _f:
                        _json.dump({
                            'status': 'waiting_2fa', 'url': cur_url_after_pw,
                            'debug_port': UC_DEBUG_PORT,
                            'screenshot': f"{screenshot_dir}/uc_2fa_start.png",
                            'timestamp': time.time(),
                        }, _f)
                    log(f"[uc] 📡 Signal written (monitoring only) | DevTools :{UC_DEBUG_PORT}")

                    # ── Anti-race: check if CDP handler already completed 2FA ──
                    # stealth-runner.mjs writes /tmp/xio_uc_2fa_resume.json with
                    # {status:'done'} when handle2FAViaCDP() succeeds.  If that
                    # file exists before we even start the inline handler, the
                    # CDP path already took care of TOTP — skip to post-login.
                    _resume_path = resume_file
                    _cdp_done = False
                    _resume_wait_start = time.time()
                    # Give CDP handler up to 8s to run first (it runs in parallel).
                    # Device push screens (Check your phone) can't be handled by CDP
                    # — fall through quickly so the inline "Try another way" click runs.
                    while time.time() - _resume_wait_start < 8:
                        if os.path.exists(_resume_path):
                            try:
                                _rd = _json.loads(open(_resume_path).read())
                                if _rd.get('status') == 'done':
                                    _cdp_done = True
                                    log("[uc] ✅ CDP handler already completed 2FA — skipping inline TOTP")
                                elif _rd.get('status') == 'error':
                                    log(f"[uc] ⚠️ CDP handler reported error: {_rd.get('msg','?')} — running inline fallback")
                            except Exception:
                                pass
                            break
                        time.sleep(1.0)

                    # Immediately run inline 2FA handler — only if CDP did NOT already handle it
                    if not _cdp_done:
                        log("[uc] ⚡ Running inline 2FA handler...")
                    # ── reCAPTCHA short-circuit: click checkbox directly, skip TOTP flow ──
                    # Clicking 'Try another way' on the reCAPTCHA page causes account rejection.
                    if 'challenge/recaptcha' in cur_url_after_pw and not _cdp_done:
                        log("[uc] 🤖 reCAPTCHA challenge detected inline — clicking checkbox...")
                        if handle_recaptcha_buster(driver, log, uc_sleep):
                            log("[uc] ✅ reCAPTCHA resolved inline — proceeding to post-login check")
                            uc_sleep(3.0, 5.0)
                            # Check if Google now needs password (email→recaptcha→pwd flow)
                            _url_after_rc = driver.current_url
                            if 'challenge/pwd' in _url_after_rc or 'signin/v2/challenge/pwd' in _url_after_rc:
                                log(f"[uc] 🔑 Password challenge after reCAPTCHA — entering password... ({_url_after_rc[:60]})")
                                try:
                                    from selenium.webdriver.support.ui import WebDriverWait
                                    from selenium.webdriver.support import expected_conditions as EC
                                    from selenium.webdriver.common.keys import Keys
                                    _pw_wait = WebDriverWait(driver, 12)
                                    _pw_el = _pw_wait.until(EC.visibility_of_element_located(
                                        (By.CSS_SELECTOR, "input[type='password']")))
                                    _pw_el.click()
                                    uc_sleep(0.4, 0.8)
                                    uc_type(_pw_el, password)
                                    _pw_el.send_keys(Keys.RETURN)
                                    # Wait up to 15s for URL to leave challenge/pwd
                                    import time as _tm2
                                    _nav_deadline = _tm2.time() + 15
                                    while 'challenge/pwd' in driver.current_url and _tm2.time() < _nav_deadline:
                                        _tm2.sleep(0.5)
                                    uc_sleep(1.0, 1.5)
                                    uc_snap(driver, f"{screenshot_dir}/uc_pwd_after_recaptcha.jpg")
                                    log("[uc] 📸 uc_pwd_after_recaptcha.jpg")
                                    _url_post_pwd = driver.current_url
                                    log(f"[uc] Post-pwd URL: {_url_post_pwd[:80]}")
                                    if ('challenge/pwd' not in _url_post_pwd and
                                            totp_secret and
                                            any(k in _url_post_pwd for k in ['challenge','2sv','totp','selection','lookup'])):
                                        log(f"[uc] 🔐 2FA after reCAPTCHA+pwd — entering TOTP...")
                                        try:
                                            # Step 1: ‘Try another way’ — JS multi-event (exact Branch B / c66f366)
                                            _rc_taw = driver.execute_script("""
return (function(){
  var all=document.querySelectorAll('button,div[role="button"],div[role="link"],a,span');
  for(var i=0;i<all.length;i++){
var t=(all[i].innerText||'').toLowerCase();
if(t.includes('try another way')||t.includes('more options')){
  all[i].scrollIntoView({block:'center'});
  ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(ev){
    all[i].dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
  }); return true;
}
  } return false;
})()""")
                                            if _rc_taw:
                                                log("[uc] 🔄 'Try another way' → multi-event dispatched")
                                                uc_sleep(2.0, 3.0)
                                                uc_snap(driver, f"{screenshot_dir}/uc_rc_try_another.jpg")
                                                log("[uc] 📸 uc_rc_try_another.jpg")
                                                _rc_auth = driver.execute_script("""
return (function(){
  var t=document.querySelector('div[data-challengetype="6"]');
  if(!t){
var els=document.querySelectorAll('div[role="link"],div[role="button"],li');
for(var i=0;i<els.length;i++){
  if((els[i].innerText||'').toLowerCase().includes('authenticator')){t=els[i];break;}
}
  }
  if(t){
t.scrollIntoView({block:'center'});
['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(ev){
  t.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
}); return true;
  } return false;
})()""")
                                                if _rc_auth:
                                                    log("[uc] ✅ Authenticator selected (data-challengetype=6)")
                                                    uc_sleep(2.5, 3.5)
                                                    uc_snap(driver, f"{screenshot_dir}/uc_rc_auth_selected.jpg")
                                                    log("[uc] 📸 uc_rc_auth_selected.jpg")
                                                else:
                                                    log("[uc] ⚠️ Authenticator option not found in menu")
                                            else:
                                                log("[uc] ℹ️ No 'Try another way' — TOTP may be direct")
                                            _rc_totp_el = driver.execute_script("""
return (function(){
  var inps=document.querySelectorAll('input:not([type="hidden"])');
  for(var i=0;i<inps.length;i++){
var r=inps[i].getBoundingClientRect();
if(r.width>0&&r.height>0){
  var a=(inps[i].id+' '+inps[i].name+' '+(inps[i].getAttribute('aria-label')||'')).toLowerCase();
  if(a.includes('pin')||a.includes('totp')||a.includes('code')||inps[i].type==='tel') return inps[i];
}
  }
  for(var i=0;i<inps.length;i++){
var r=inps[i].getBoundingClientRect();
if(r.width>0&&r.height>0&&(inps[i].type==='tel'||inps[i].type==='number')) return inps[i];
  }
  return null;
})()""")
                                            if _rc_totp_el is None:
                                                log("[uc] JS rect check missed — trying Selenium wait")
                                                try:
                                                    _rc_totp_el = WebDriverWait(driver, 10).until(
                                                        EC.visibility_of_element_located((By.CSS_SELECTOR,
                                                            "input[type='tel'],input[type='number'],"
                                                            "input[name='totpPin'],input[autocomplete='one-time-code']")))
                                                except Exception:
                                                    _rc_totp_el = None
                                            if _rc_totp_el:
                                                _rc_code = generate_totp(totp_secret)
                                                log(f"[uc] ⌨️  TOTP {_rc_code} → robust clear + type")
                                                try: driver.execute_script("arguments[0].value='';arguments[0].dispatchEvent(new Event('input',{bubbles:true}));",_rc_totp_el)
                                                except Exception: pass
                                                try:
                                                    from selenium.webdriver.common.action_chains import ActionChains
                                                    ActionChains(driver).triple_click(_rc_totp_el).send_keys(Keys.DELETE).perform()
                                                    uc_sleep(0.2, 0.3)
                                                except Exception: pass
                                                try: _rc_totp_el.send_keys(Keys.CONTROL+'a'); _rc_totp_el.send_keys(Keys.DELETE); uc_sleep(0.1,0.2)
                                                except Exception: pass
                                                try: _rc_totp_el.send_keys(Keys.END); _rc_totp_el.send_keys(Keys.BACK_SPACE*20); uc_sleep(0.1,0.2)
                                                except Exception: pass
                                                _rc_totp_el.send_keys(_rc_code)
                                                uc_sleep(0.4, 0.8)
                                                _rc_next = driver.execute_script("""
return (function(){
  var b=document.querySelector('#totpNext,button[jsname]');
  if(b){['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(ev){
    b.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
  });return true;} return false;
})()""")
                                                if not _rc_next: _rc_totp_el.send_keys(Keys.RETURN)
                                                log("[uc] ✅ TOTP submitted (reCAPTCHA+pwd flow)")
                                                uc_sleep(5.0, 7.0)
                                                uc_snap(driver, f"{screenshot_dir}/uc_totp_after_recaptcha.jpg")
                                                log("[uc] 📸 uc_totp_after_recaptcha.jpg")
                                            else:
                                                log("[uc] ⚠️ TOTP input not found after reCAPTCHA+pwd")
                                        except Exception as _rc_totp_err:
                                            log(f"[uc] ⚠️ TOTP (reCAPTCHA+pwd) failed: {_rc_totp_err}")
                                except Exception as _pwd_err:
                                    log(f"[uc] ⚠️ Password after reCAPTCHA failed: {_pwd_err}")
                        else:
                            log("[uc] ⚠️  reCAPTCHA inline handler failed")
                    elif totp_secret and not _cdp_done:
                        try:
                            from selenium.webdriver.common.keys import Keys

                            # JS multi-event dispatch — ported from XIO_VERSE 2 auth_manager.py
                            # Avoids Selenium .click() renderer crash on Google 2FA pages
                            def js_multi_click(selector_or_el):
                                if isinstance(selector_or_el, str):
                                    return driver.execute_script("""
(function(sel){
  var el=document.querySelector(sel); if(!el) return false;
  el.scrollIntoView({block:'center'});
  ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(ev){
el.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
  }); return true;
})(arguments[0])""", selector_or_el)
                                else:
                                    return driver.execute_script("""
(function(el){
  el.scrollIntoView({block:'center'});
  ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(ev){
el.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
  }); return true;
})(arguments[0])""", selector_or_el)

                            # Step 1: "Try another way" — JS multi-event
                            taw = driver.execute_script("""
return (function(){
  var all=document.querySelectorAll('button,div[role="button"],div[role="link"],a,span');
  for(var i=0;i<all.length;i++){
var t=(all[i].innerText||'').toLowerCase();
if(t.includes('try another way')||t.includes('more options')){
  all[i].scrollIntoView({block:'center'});
  ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(ev){
    all[i].dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
  }); return true;
}
  } return false;
})()""")
                            if taw:
                                log("[uc] 🔄 'Try another way' → multi-event dispatched")
                                uc_sleep(2.0, 3.0)
                                uc_snap(driver, f"{screenshot_dir}/uc_try_another.jpg")

                                # Step 2: Authenticator — data-challengetype="6"
                                auth = driver.execute_script("""
return (function(){
  var t=document.querySelector('div[data-challengetype="6"]');
  if(!t){
var els=document.querySelectorAll('div[role="link"],div[role="button"],li');
for(var i=0;i<els.length;i++){
  if((els[i].innerText||'').toLowerCase().includes('authenticator')){t=els[i];break;}
}
  }
  if(t){
t.scrollIntoView({block:'center'});
['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(ev){
  t.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
}); return true;
  } return false;
})()""")
                                if auth:
                                    log("[uc] ✅ Authenticator option selected (data-challengetype=6)")
                                    uc_sleep(2.5, 3.5)
                                    uc_snap(driver, f"{screenshot_dir}/uc_auth_selected.jpg")
                                else:
                                    log("[uc] ⚠️ Authenticator option not found in menu")
                            else:
                                log("[uc] ℹ️ No 'Try another way' — TOTP may be direct")

                            # Step 3: Find visible TOTP input (XIO_VERSE getBoundingClientRect approach)
                            totp_el = driver.execute_script("""
return (function(){
  var inps=document.querySelectorAll('input:not([type="hidden"])');
  for(var i=0;i<inps.length;i++){
var r=inps[i].getBoundingClientRect();
if(r.width>0&&r.height>0){
  var a=(inps[i].id+' '+inps[i].name+' '+(inps[i].getAttribute('aria-label')||'')).toLowerCase();
  if(a.includes('pin')||a.includes('totp')||a.includes('code')||inps[i].type==='tel') return inps[i];
}
  }
  for(var i=0;i<inps.length;i++){
var r=inps[i].getBoundingClientRect();
if(r.width>0&&r.height>0&&(inps[i].type==='tel'||inps[i].type==='number')) return inps[i];
  }
  return null;
})()""")
                            if totp_el is None:
                                log("[uc] JS rect check missed — trying Selenium wait")
                                try:
                                    totp_el = WebDriverWait(driver, 10).until(
                                        EC.visibility_of_element_located((By.CSS_SELECTOR,
                                            "input[type='tel'],input[type='number'],"
                                            "input[name='totpPin'],input[autocomplete='one-time-code']"
                                        ))
                                    )
                                except Exception:
                                    totp_el = None

                            if totp_el:
                                code = generate_totp(totp_secret)
                                log(f"[uc] ⌨️  TOTP {code} → robust clear + human type")

                                # ── Robust field clear (4 strategies) ─────────────
                                # Google's TOTP input ignores selenium .clear() —
                                # use JS reset + keyboard clear to guarantee empty field.
                                try:
                                    # S1: JS direct value reset (fastest, works on plain inputs)
                                    driver.execute_script(
                                        "arguments[0].value = ''; "
                                        "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));",
                                        totp_el
                                    )
                                except Exception:
                                    pass
                                try:
                                    # S2: Triple-click to select all text, then Delete
                                    from selenium.webdriver.common.action_chains import ActionChains
                                    ActionChains(driver).triple_click(totp_el).send_keys(Keys.DELETE).perform()
                                    uc_sleep(0.2, 0.3)
                                except Exception:
                                    pass
                                try:
                                    # S3: Ctrl+A to select all, then Delete
                                    totp_el.send_keys(Keys.CONTROL + 'a')
                                    totp_el.send_keys(Keys.DELETE)
                                    uc_sleep(0.1, 0.2)
                                except Exception:
                                    pass
                                try:
                                    # S4: Backspace loop (last resort — clears up to 20 chars)
                                    totp_el.send_keys(Keys.END)
                                    totp_el.send_keys(Keys.BACK_SPACE * 20)
                                    uc_sleep(0.1, 0.2)
                                except Exception:
                                    pass

                                # Verify field is empty before typing
                                try:
                                    _cur_val = totp_el.get_attribute('value') or ''
                                    if _cur_val:
                                        log(f"[uc] ⚠️ Field still has '{_cur_val}' after clear — JS force")
                                        driver.execute_script("arguments[0].value = '';", totp_el)
                                except Exception:
                                    pass

                                uc_sleep(0.3, 0.5)
                                totp_el.send_keys(code)
                                uc_sleep(0.5, 0.8)

                                # Verify typed value matches expected
                                try:
                                    _typed = totp_el.get_attribute('value') or ''
                                    if _typed != code:
                                        log(f"[uc] ⚠️ Field shows '{_typed}' but expected '{code}' — re-clearing")
                                        driver.execute_script("arguments[0].value = arguments[1];", totp_el, code)
                                        driver.execute_script(
                                            "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));",
                                            totp_el
                                        )
                                    else:
                                        log(f"[uc] ✅ Field confirmed: '{_typed}'")
                                except Exception:
                                    pass

                                # Submit: try Next button via JS, else ENTER
                                next_clicked = False
                                try:
                                    nb = driver.find_element(By.XPATH,
                                        "//button[contains(.,'Next')]|//div[@id='totpNext']"
                                        "|//div[@id='idvPreregisteredPhoneNext']"
                                    )
                                    js_multi_click(nb)
                                    next_clicked = True
                                    log("[uc] ✅ TOTP Next button → JS multi-event")
                                except Exception:
                                    pass
                                if not next_clicked:
                                    totp_el.send_keys(Keys.RETURN)
                                    log("[uc] ✅ TOTP submitted via ENTER key")
                                uc_sleep(5.0, 7.0)
                                uc_snap(driver, f"{screenshot_dir}/uc_after_totp.jpg")
                            else:
                                log("[uc] ⚠️ No TOTP input found anywhere")

                        except Exception as e:
                            log(f"[uc] ⚠️ Inline 2FA error: {e}")
                            uc_snap(driver, f"{screenshot_dir}/uc_2fa_error.jpg")

                    if os.path.exists(signal_path): os.remove(signal_path)

                # ── Post-login: Skip recovery/address/promo prompts ───────────
                # Ported from XIO_VERSE 2 auth_manager.py lines 309-317
                for _ in range(3):
                    try:
                        # Buster: detect and solve reCAPTCHA if present
                        if handle_recaptcha_buster(driver, log, uc_sleep):
                            uc_sleep(2.0, 3.0)
                            continue  # re-check page after captcha solve
                    except Exception:
                        pass
                    try:
                        uc_sleep(1.0, 2.0)
                        ps_lower = driver.page_source.lower()
                        if any(x in ps_lower for x in ['recovery','make sure you can always sign in',
                            'protect your account','passkey','add a phone number','not now', 'home address', 'set a home address']):
                            log("[uc] 🛡️ Post-login prompt — hunting Skip/Cancel...")
                            try:
                                from selenium.webdriver.common.keys import Keys as _K
                                skip = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((
                                    By.XPATH,
                                    "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'cancel')"
                                    " or contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'not now')"
                                    " or contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'skip')"
                                    " or contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'no thanks')]"
                                )))
                                skip.send_keys(_K.RETURN)
                                log("[uc] ✅ Skip/Cancel clicked")
                                uc_sleep(2.0, 3.0)
                            except Exception as e:
                                log(f"[uc] ⚠️ Failed to click Skip/Cancel: {e}")
                                break
                        else:
                            break
                    except Exception as e:
                        log(f"[uc] ⚠️ Post-login check failed: {e}")
                        break


            # Verify
            driver.get("https://myaccount.google.com/")
            uc_sleep(2.0, 3.0)
            # Handle reCAPTCHA that may appear at the verification page
            if handle_recaptcha_buster(driver, log, uc_sleep):
                log("[uc] ✅ reCAPTCHA at final page resolved — re-navigating to myaccount")
                driver.get("https://myaccount.google.com/")
                uc_sleep(2.0, 3.0)
            uc_snap(driver, f"{screenshot_dir}/uc_final.jpg")

            final_url = driver.current_url
            final_title = driver.title
            # Accept any Google Account domain (myaccount or accounts) as a successful login.
            # Google may redirect to accounts.google.com/overview instead of myaccount.google.com.
            _google_acct_domains = ("myaccount.google.com", "accounts.google.com")
            _is_logged_in_url = any(d in final_url for d in _google_acct_domains)
            _is_not_signin = "Sign in" not in final_title and "signin" not in final_url
            if _is_logged_in_url and _is_not_signin:
                # ── Extract ALL cookies via CDP (not domain-restricted) ───────────────
                try:
                    cdp_result = driver.execute_cdp_cmd("Network.getAllCookies", {})
                    raw_cookies_cdp = cdp_result.get("cookies", [])
                    log(f"[uc] 🍪 CDP getAllCookies: {len(raw_cookies_cdp)} cookies (all domains)")
                    raw_cookies = raw_cookies_cdp
                except Exception as _ce:
                    log(f"[uc] ⚠️ CDP cookie fallback: {_ce} — using driver.get_cookies()")
                    raw_cookies = driver.get_cookies()

                cookies = []
                for c in raw_cookies:
                    cookies.append({
                        "name":     c.get("name", ""),
                        "value":    c.get("value", ""),
                        "domain":   c.get("domain", ""),
                        "path":     c.get("path", "/"),
                        "expires":  c.get("expiry", c.get("expires", -1)),
                        "httpOnly": c.get("httpOnly", False),
                        "secure":   c.get("secure", False),
                        "sameSite": "Lax",
                    })
                write_session(cookies, output_path)

                # ── Archive signed-in Chrome profile BEFORE quit ──────────────────────
                # Navigate to blank to flush writes before tarring
                if profile_dir and os.path.isdir(profile_dir):
                    try:
                        # Pause Chrome I/O so tar can read cleanly
                        try:
                            driver.get("about:blank")
                            uc_sleep(1.5, 2.0)
                        except Exception:
                            pass

                        profiles_root = os.path.dirname(profile_dir)
                        profile_name  = os.path.basename(profile_dir)
                        tar_path = os.path.join(profiles_root, f'{profile_name}.tar.gz')
                        import subprocess as _sp
                        r = _sp.run(
                            ['tar', '--ignore-failed-read',
                             '--exclude=SingletonLock', '--exclude=SingletonCookie',
                             '--exclude=*.lock', '--exclude=lockfile',
                             '-czf', tar_path, '-C', profiles_root, profile_name],
                            capture_output=True, text=True, timeout=120
                        )
                        # returncode 1 = "some files changed" warning, archive still valid
                        sz = os.path.getsize(tar_path) if os.path.exists(tar_path) else 0
                        if r.returncode in (0, 1) and sz > 10000:
                            log(f'[uc] 💾 Profile archived: {tar_path} ({sz:,}b)')
                        else:
                            log(f'[uc] ⚠️  Profile archive incomplete: code={r.returncode} size={sz}b err={r.stderr[:80]}')
                    except Exception as _pe:
                        log(f'[uc] ⚠️  Profile archive skipped: {_pe}')

                try: driver.quit()
                except Exception: pass
                return True
            else:
                log(f"[uc] \u274c Verification failed: {final_url[:80]}")
                try: driver.quit()
                except Exception: pass
                return False
        except Exception as e:
            log(f"[uc] ❌ Exception in flow: {e}")
            try: driver.quit()
            except Exception: pass
            return False
    except Exception as e:
        log(f"[uc] ❌ Launch failed: {e}")
        return False

# ══════════════════════════════════════════════════════════════════════════════
# ENGINE 3: nodriver — modern uc successor, no WebDriver, pure CDP
# ══════════════════════════════════════════════════════════════════════════════

async def run_nodriver(email, password, totp_secret, socks5, output_path, screenshot_dir):
    log("[nodriver] Starting nodriver session...")

    async def _nd_close(b):
        """Safely close/stop a nodriver Browser (API changed across versions)."""
        for method in ('stop', 'close'):
            fn = getattr(b, method, None)
            if callable(fn):
                try:
                    result = fn()
                    if asyncio.iscoroutine(result):
                        await result
                    return
                except Exception as ce:
                    log(f"[nodriver] browser.{method}() warning: {ce}")
        log("[nodriver] ⚠️  No close/stop method found on browser object")

    try:
        import nodriver as nd
    except ImportError:
        log("[nodriver] Installing nodriver...")
        os.system("pip install nodriver -q")
        try:
            import nodriver as nd
        except ImportError:
            log("[nodriver] ❌ Install failed — skipping")
            return False

    import random

    try:
        proxy_args = []
        if socks5:
            proxy_addr = socks5.replace('socks5://', '')
            proxy_args = [f'--proxy-server=socks5://{proxy_addr}']

        browser = await nd.start(
            browser_args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--use-gl=angle',
                '--use-angle=swiftshader',
                '--disable-gpu-sandbox',
                '--ignore-gpu-blocklist',
                '--window-size=1920,1080',
                f'--display={DISPLAY}',
            ] + proxy_args,
            headless=False,
        )

        page = await browser.get("https://myaccount.google.com/")
        await asyncio.sleep(2)

        if "myaccount.google.com" in page.url and "signin" not in page.url:
            log("[nodriver] ✅ Already authenticated")
        else:
            # Warmup
            for url in ["https://www.google.com"]:
                try:
                    await browser.get(url)
                    await asyncio.sleep(1.5)
                except Exception: pass

            # Email — get the current active page from browser
            try:
                page = await browser.get(
                    "https://accounts.google.com/ServiceLogin?service=mail"
                    "&hl=en&continue=https://mail.google.com"
                )
                await asyncio.sleep(3.5)
                # Use query_selector_all to handle page-reference staleness
                email_els = await page.query_selector_all("input[type='email'], input[name='identifier']")
                if email_els:
                    email_el = email_els[0]
                    await email_el.click()
                    await asyncio.sleep(0.5)
                    await email_el.send_keys(email)
                    await asyncio.sleep(0.8)
                    next_els = await page.query_selector_all("#identifierNext, button[type='submit']")
                    if next_els:
                        await next_els[0].click()
                    await asyncio.sleep(4)
                else:
                    log("[nodriver] ⚠️ Email input not found — trying send_keys directly")
                    await page.send_keys(email)
                    await asyncio.sleep(0.5)
                    await page.send_keys("\n")
                    await asyncio.sleep(3)
            except Exception as e:
                log(f"[nodriver] ⚠️ Email phase: {e}")

            # Refresh page reference to get current URL
            # Guard: page.url may be None/empty after an email-phase exception.
            # Passing None to browser.get() raises "Cannot navigate to invalid URL".
            _cur_url = getattr(page, 'url', None)
            if _cur_url and isinstance(_cur_url, str) and _cur_url.startswith('http'):
                page = await browser.get(_cur_url)
            # else: keep existing page reference (no navigation needed)
            await asyncio.sleep(1)

            if "rejected" in page.url:
                await page.save_screenshot(f"{screenshot_dir}/nodriver_rejected.png")
                log("[nodriver] ❌ Bot-detection redirect")
                await _nd_close(browser)
                return False

            # Password
            try:
                pw_el = await page.find("input[type='password']", timeout=8)
                await pw_el.click()
                await asyncio.sleep(0.5)
                await page.send_keys(password)
                await asyncio.sleep(0.5)
                await page.send_keys('\n')
                await asyncio.sleep(0.7)
                try:
                    next_btn = await page.find("#passwordNext", timeout=5)
                    await next_btn.click()
                except:
                    pass
                await asyncio.sleep(4)
            except Exception as e:
                log(f"[nodriver] ⚠️ Password phase: {e}")

            # TOTP
            try:
                body_text = await page.get_content()
                if totp_secret and any(k in body_text for k in ["2-Step", "verification code"]):
                    code = generate_totp(totp_secret)
                    log(f"[nodriver] 2FA entering TOTP {code}...")
                    totp_el = await page.find("input[type='tel'], input[name='totpPin']", timeout=8)
                    await totp_el.apply(f"(el) => {{ el.value = '{code}'; el.dispatchEvent(new Event('input', {{bubbles: true}})); el.dispatchEvent(new Event('change', {{bubbles: true}})); }}")
                    await asyncio.sleep(0.6)
                    await totp_el.send_keys('\\n')
                    await asyncio.sleep(0.6)
                    next_btn = await page.find("#totpNext", timeout=5)
                    await next_btn.click()
                    await asyncio.sleep(4)
            except Exception as e:
                log(f"[nodriver] ⚠️ TOTP phase: {e}")

        # Post-login prompts
        for _ in range(3):
            try:
                await asyncio.sleep(1.5)
                body_text = await page.get_content()
                ps_lower = body_text.lower()
                if any(x in ps_lower for x in ['recovery','make sure you can always sign in',
                    'protect your account','passkey','add a phone number','not now', 'home address', 'set a home address']):
                    log("[nodriver] 🛡️ Post-login prompt — hunting Skip/Cancel...")
                    try:
                        skip_btns = await page.find_all("button, div[role='button'], a, span", timeout=2)
                        clicked = False
                        for btn in skip_btns:
                            t = (btn.text_all or "").lower()
                            if any(k in t for k in ['cancel', 'not now', 'skip', 'no thanks']):
                                await btn.click()
                                log("[nodriver] ✅ Skip/Cancel clicked")
                                clicked = True
                                break
                        if not clicked:
                            break
                    except Exception as e:
                        log(f"[nodriver] ⚠️ Failed to click Skip/Cancel: {e}")
                        break
                else:
                    break
            except Exception as e:
                log(f"[nodriver] ⚠️ Post-login check failed: {e}")
                break

        # Verify
        page = await browser.get("https://myaccount.google.com/")
        await asyncio.sleep(2)
        await page.save_screenshot(f"{screenshot_dir}/nodriver_final.png")

        if "myaccount.google.com" in page.url:
            cookies = await browser.cookies()
            write_session([c.__dict__ for c in cookies], output_path)
            await _nd_close(browser)
            return True
        log("[nodriver] ❌ Verification failed")
        await _nd_close(browser)
        return False
    except Exception as e:
        log(f"[nodriver] ❌ Exception: {e}")
        return False

# ══════════════════════════════════════════════════════════════════════════════
# MAIN — Multi-engine runner
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email",           required=True)
    parser.add_argument("--password",        required=True)
    parser.add_argument("--totp_secret",     default="")
    parser.add_argument("--socks5",          default="socks5://127.0.0.1:1055")
    parser.add_argument("--output",          required=True)
    parser.add_argument("--engines",         default="uc",
                        help="Comma-separated ordered engine list (UC-only mode: only 'uc' is active)")
    parser.add_argument("--screenshot_dir",  default="/tmp/xio_screenshots")
    parser.add_argument("--profile_dir",     default="",
                        help="Chrome user-data-dir path — produces a signed-in profile on success")
    parser.add_argument("--workflow_id",     default="google-signin")
    parser.add_argument("--domain",          default="accounts.google.com")
    parser.add_argument("--signal_file",     default="/tmp/xio_uc_2fa_signal.json")
    parser.add_argument("--resume_file",     default="/tmp/xio_uc_2fa_resume.json")
    args = parser.parse_args()

    os.makedirs(args.screenshot_dir, exist_ok=True)
    engines = [e.strip() for e in args.engines.split(',') if e.strip()]

    log(f"[stealth-login] Starting with engine order: {' → '.join(engines)}")
    log(f"[stealth-login] Target: {args.email} / workflow: {args.workflow_id}")

    results = {}
    for engine in engines:
        log(f"\n[stealth-login] {'='*50}")
        log(f"[stealth-login] Attempting engine: {engine}")
        log(f"[stealth-login] {'='*50}")

        ok = False
        try:
            if engine == 'camoufox':
                ok = await run_camoufox(
                    args.email, args.password, args.totp_secret,
                    args.socks5, args.output, args.screenshot_dir
                )
            elif engine == 'uc':
                # run_uc is synchronous — run in executor
                loop = asyncio.get_event_loop()
                import functools as _ft
                ok = await loop.run_in_executor(None, _ft.partial(
                    run_uc,
                    args.email, args.password, args.totp_secret,
                    args.socks5, args.output, args.screenshot_dir,
                    args.profile_dir or None,
                    args.signal_file, args.resume_file
                ))
            elif engine == 'nodriver':
                ok = await run_nodriver(
                    args.email, args.password, args.totp_secret,
                    args.socks5, args.output, args.screenshot_dir
                )
        except Exception as e:
            log(f"[stealth-login] ❌ {engine} threw exception: {e}")
            ok = False

        results[engine] = ok
        # Emit machine-readable result line for Node.js to parse
        print(f"__ESR_RESULT__ {engine} {'success' if ok else 'fail'}", flush=True)

        if ok:
            log(f"\n[stealth-login] ✅ SUCCESS via {engine}")
            # Final summary JSON for workflow caller
            print(json.dumps({
                "success": True,
                "engine":  engine,
                "output":  args.output,
            }), flush=True)
            sys.exit(0)

    log(f"\n[stealth-login] ❌ All engines failed: {results}")
    print(json.dumps({"success": False, "engines_tried": results}), flush=True)
    sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())