"""colab.agent.browser.uc_login_core — Undetected-chromedriver stealth Google login flow.

Decomposed login execution pipeline handling:
- Device emulation (CDP timezone, geo, UA, stealth scripts)
- Pre-auth verification shortcut (Phase 0)
- ServiceLogin & Google warmup navigation
- Identifier input (Phase 1)
- Password input (Phase 2)
- 2FA / TOTP / reCAPTCHA / Challenge handling with HITL (Phase 3)
- Post-login interstitials and account prompts (Phase 4)
- Verification and complete cookie jar capture via CDP (Phase 5)
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any

from selenium.webdriver.common.by import By

from colab.agent.browser.cdp_primitives import (
    cdp_clear_input,
    cdp_click_element,
    cdp_eval,
    cdp_get_rect,
    cdp_mouse_move,
    cdp_type_text,
    safe_get,
    uc_sleep,
)
from colab.agent.browser.uc_launcher import _launch_uc_chrome_for_login
from colab.agent.config import NODE_NAME, _CHROME_VERSION, _NOVNC_URL, XIOSYNC_BASE
from colab.agent.models import HITLNotice, HITLState
from colab.agent.network.geo import _resolve_ip_geo
from colab.agent.state import _hitl_store
from colab.agent.stealth.stealth_js import _STEALTH_JS

logger = logging.getLogger("xiorun_agent.browser.uc_login_core")


def rnd(a: float = 0.03, b: float = 0.09) -> float:
    """Return randomized uniform jitter float."""
    return random.uniform(a, b)


def find(driver: Any, selectors: str | list[str], t: float = 15) -> Any:
    """Find visible DOM element using a list of CSS selectors."""
    if isinstance(selectors, str):
        selectors = [selectors]
    deadline = time.time() + t
    while time.time() < deadline:
        for sel in selectors:
            try:
                el = driver.find_element("css selector", sel)
                if el.is_displayed():
                    return el
            except Exception:
                pass
        time.sleep(0.4)
    url = getattr(driver, "current_url", "")
    logger.warning(f"uc-login find_failed sel={selectors} url={url[:80]}")
    raise Exception(f"Element not found: {selectors} (url={url[:80]})")


# ── Phase 0: Pre-Auth Check ───────────────────────────────────────────────────
def _phase_preauth_check(
    driver: Any,
    email: str,
    user_data_dir: str | None = None,
    uc_debug_port: int = 0,
    mouse_pos: dict[str, int | float] | None = None,
) -> dict[str, Any] | None:
    """Check if profile is already authenticated on myaccount.google.com."""
    logger.info("uc-login: Phase 0 — checking myaccount.google.com")
    try:
        driver.get("https://myaccount.google.com/")
    except Exception as p0e:
        logger.warning(f"uc-login: Phase 0 nav exception ({str(p0e)[:80]}) — Chrome recovering...")
        time.sleep(3)

    uc_sleep(driver, 1.5, 2.5, mouse_pos=mouse_pos)

    phase0_url = driver.current_url
    is_myaccount = "myaccount.google.com" in phase0_url
    if is_myaccount and "signin" not in phase0_url:
        try:
            email_check = driver.execute_cdp_cmd(
                "Runtime.evaluate",
                {
                    "expression": f"document.body.innerText.toLowerCase().includes('{email.lower()}')",
                    "returnByValue": True,
                    "timeout": 3000,
                },
            )
            email_found = email_check.get("result", {}).get("value", True)
            logger.info(
                f"uc-login: Phase 0 email check → found={email_found} url={phase0_url[:60]}"
            )
        except Exception as ec:
            email_found = True
            logger.warning(
                f"uc-login: Phase 0 email check failed ({ec}) — assuming correct account"
            )

        if email_found:
            logger.info(
                "uc-login: already authenticated ✅ — email verified on page (Phase 0 shortcut)"
            )
            try:
                cdp_cookies = driver.execute_cdp_cmd("Network.getAllCookies", {}).get(
                    "cookies", []
                )
            except Exception:
                cdp_cookies = driver.get_cookies()
            return {
                "ok": True,
                "uc_port": uc_debug_port,
                "profile_dir": user_data_dir,
                "cookies": cdp_cookies,
                "final_url": driver.current_url,
            }
        else:
            logger.warning(
                f"uc-login: Phase 0 — WRONG ACCOUNT on myaccount page (expected {email}) — forcing full re-login"
            )
    else:
        logger.info(
            f"uc-login: Phase 0 — not authenticated ({phase0_url[:80]}), proceeding to login"
        )
    return None


# ── Navigation & Account Chooser ──────────────────────────────────────────────
def _phase_service_login_nav(
    driver: Any,
    email: str,
    mouse_pos: dict[str, int | float] | None = None,
) -> bool:
    """Warmup and navigate to Google ServiceLogin. Returns True if pre-authed."""
    logger.info("uc-login: warmup — google.com")
    safe_get(driver, "https://www.google.com", wait=1.0, mouse_pos=mouse_pos)

    logger.info("uc-login: navigating to ServiceLogin")
    sl_url = (
        "https://accounts.google.com/ServiceLogin"
        "?service=mail&hl=en&continue=https://mail.google.com"
    )
    sl_ok = False
    for sl_attempt in range(3):
        try:
            driver.get(sl_url)
        except Exception as sg_err:
            logger.warning(
                f"uc-login: ServiceLogin nav exception attempt {sl_attempt + 1}: {str(sg_err)[:120]} — retrying"
            )
        sl_wait = 3.0 + sl_attempt * 2.0
        time.sleep(sl_wait)
        curr_start = driver.current_url
        logger.info(f"uc-login: post-nav url attempt {sl_attempt + 1}: {curr_start}")
        google_service = any(
            s in curr_start
            for s in [
                "accounts.google.com",
                "google.com/ServiceLogin",
                "mail.google.com",
                "drive.google.com",
                "docs.google.com",
                "myaccount.google.com",
                "youtube.com",
            ]
        )
        if google_service:
            sl_ok = True
            break
        logger.warning(
            f"uc-login: still on {curr_start[:60]} after attempt {sl_attempt + 1}, retrying"
        )
        time.sleep(2.0)

    curr_start = driver.current_url
    if not sl_ok:
        google_service = any(
            s in curr_start
            for s in [
                "accounts.google.com",
                "google.com/ServiceLogin",
                "mail.google.com",
                "drive.google.com",
                "myaccount.google.com",
            ]
        )
        if not google_service and ("chrome://" in curr_start or "new-tab" in curr_start):
            raise Exception(
                f"ServiceLogin unreachable after 3 attempts — "
                f"still on {curr_start[:80]}. Check proxy connectivity."
            )

    curr_path = curr_start.split("?")[0]
    already_authed = any(
        s in curr_path
        for s in [
            "mail.google.com",
            "drive.google.com",
            "docs.google.com",
            "myaccount.google.com",
            "youtube.com",
        ]
    )
    if already_authed:
        logger.info(
            f"uc-login: ✅ profile pre-authenticated → {curr_start[:80]} — skipping login form"
        )
        return True

    # Handle accountchooser
    if "accountchooser" in curr_start or ("v3/signin" in curr_start and "accountchooser" in curr_start):
        logger.info("uc-login: on accountchooser — clicking 'Use another account'")
        try:
            clicked_other = False
            for aci in range(10):
                try:
                    res = driver.execute_script(
                        """
                        var btns = document.querySelectorAll('[data-identifier]');
                        if (btns.length > 0) {
                            var email = arguments[0];
                            for (var b of btns) {
                                if (b.getAttribute('data-identifier') === email) {
                                    b.click();
                                    return 'clicked_account';
                                }
                            }
                        }
                        var others = document.querySelectorAll('[data-action="use_another_account"]');
                        if (others.length > 0) { others[0].click(); return 'use_another'; }
                        var links = Array.from(document.querySelectorAll('li, [role="listitem"]'));
                        for (var l of links) {
                            if (l.textContent.includes('Use another account') || l.textContent.includes('another account')) {
                                l.click(); return 'text_click';
                            }
                        }
                        return 'not_found';
                    """,
                        email,
                    )
                    logger.info(f"uc-login: accountchooser JS result={res}")
                    if res in ("clicked_account", "use_another", "text_click"):
                        clicked_other = True
                        time.sleep(2.5)
                        curr_start = driver.current_url
                        logger.info(f"uc-login: after chooser nav → {curr_start[:80]}")
                        break
                except Exception as ace:
                    logger.debug(f"uc-login: accountchooser JS attempt {aci}: {ace}")
                time.sleep(0.5)
            if not clicked_other:
                logger.warning("uc-login: could not handle accountchooser — proceeding anyway")
        except Exception as ac_err:
            logger.warning(f"uc-login: accountchooser handler error: {ac_err}")

    return False


# ── Phase 1: Identifier (Email) ───────────────────────────────────────────────
def _phase_identifier(
    driver: Any,
    email: str,
    mouse_pos: dict[str, int | float] | None = None,
) -> dict[str, Any] | None:
    """Enter email into identifier input. Returns error dict if rejected, else None."""
    curr_start = driver.current_url
    react_ready = False
    for ri in range(20):
        try:
            inp_count = driver.execute_script(
                'return document.querySelectorAll(\'input[name="identifier"],input[type="email"],#identifierId\').length'
            )
            if inp_count and int(inp_count) > 0:
                react_ready = True
                logger.info(f"uc-login: email input found after {(ri + 1) * 0.5:.1f}s")
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not react_ready:
        logger.warning("uc-login: email input not found via poll — attempting find() anyway")

    if "/challenge/pwd" in curr_start or "/challenge/" in curr_start:
        logger.info(f"uc-login: skipped email — on challenge page {curr_start[:80]}")
    else:
        cdp_click_element(
            driver,
            [
                'input[name="identifier"]',
                'input[type="email"]',
                "#identifierId",
                "#Email",
                'input[name="Email"]',
            ],
            mouse_pos=mouse_pos,
        )
        cdp_type_text(driver, email)
        time.sleep(rnd(0.5, 1.0))
        cdp_click_element(
            driver,
            [
                "#identifierNext",
                'button[jsname="LgbsSe"]',
                'div[id="identifierNext"]',
                "#next",
                'input[id="next"]',
            ],
            mouse_pos=mouse_pos,
        )
        logger.info("uc-login: email entered")
    time.sleep(2.5)

    curr = driver.current_url
    if "/rejected" in curr or "/lookup/rejection" in curr:
        return {"ok": False, "error": f"Rejected at email step: {curr[:120]}"}

    # Handle "Choose how you want to sign in" (passkey selection screen)
    pos = mouse_pos if mouse_pos is not None else {"x": 100, "y": 100}
    for cpw_i in range(8):
        time.sleep(0.5)
        bt2 = cdp_eval(driver, "document.body.innerText||''") or ""
        if "enter your password" in bt2.lower() or "enter password" in bt2.lower():
            choose_pw_rect_js = """
            (function(){
              var all=document.querySelectorAll('*');
              for(var i=0;i<all.length;i++){
                var t=(all[i].childElementCount===0?(all[i].innerText||all[i].textContent||''):'').toLowerCase().trim();
                if(t.includes('enter your password')||t.includes('enter password')){
                  var r = all[i].getBoundingClientRect();
                  return {x:r.x, y:r.y, w:r.width, h:r.height};
                }
              } return null;
            })()
            """
            rect = cdp_eval(driver, choose_pw_rect_js)
            if rect and rect.get("w", 0) > 0:
                target_x = rect["x"] + rect["w"] * random.uniform(0.3, 0.7)
                target_y = rect["y"] + rect["h"] * random.uniform(0.3, 0.7)
                cdp_mouse_move(driver, pos["x"], pos["y"], target_x, target_y)
                pos["x"], pos["y"] = target_x, target_y
                time.sleep(random.uniform(0.05, 0.15))
                driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mousePressed",
                        "button": "left",
                        "clickCount": 1,
                        "x": int(target_x),
                        "y": int(target_y),
                    },
                )
                time.sleep(random.uniform(0.04, 0.12))
                driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseReleased",
                        "button": "left",
                        "clickCount": 1,
                        "x": int(target_x),
                        "y": int(target_y),
                    },
                )
                logger.info(
                    f"uc-login: 'Enter your password' clicked via CDP (passkey screen) — attempt {cpw_i + 1}"
                )
                time.sleep(1.5)
                break
        elif "password" in bt2.lower() and "enter" not in bt2.lower():
            break

    return None


# ── Phase 2: Password ─────────────────────────────────────────────────────────
def _phase_password(
    driver: Any,
    password: str,
    mouse_pos: dict[str, int | float] | None = None,
) -> None:
    """Enter and submit user password."""
    curr_pw = driver.current_url
    skip_pw_fill = any(k in curr_pw for k in ("challenge/recaptcha", "challenge/az"))
    if skip_pw_fill:
        logger.info(
            f"uc-login: skipping password fill — reCAPTCHA/AZ challenge active ({curr_pw[:60]})"
        )
        return

    logger.info(f"uc-login: filling password on {curr_pw[:80]}")
    try:
        cdp_click_element(
            driver,
            ['input[type="password"]', 'input[name="Passwd"]', 'input[name="password"]'],
            15,
            mouse_pos=mouse_pos,
        )
        uc_sleep(driver, 0.4, 0.8, mouse_pos=mouse_pos)
        cdp_type_text(driver, password)
        logger.info("uc-login: password typed via cdp_type_text (isTrusted)")
    except Exception as pw_err:
        logger.warning(
            f"uc-login: password CDP failed ({pw_err}) — fallback to cdp_type_text"
        )
        try:
            cdp_click_element(
                driver,
                ['input[type="password"]', 'input[name="Passwd"]', 'input[name="password"]'],
                8,
                mouse_pos=mouse_pos,
            )
            uc_sleep(driver, 0.2, 0.4, mouse_pos=mouse_pos)
            cdp_type_text(driver, password)
        except Exception as pw2:
            logger.warning(f"uc-login: cdp_type_text also failed: {pw2}")

    uc_sleep(driver, 0.5, 1.0, mouse_pos=mouse_pos)
    try:
        cdp_click_element(
            driver,
            [
                "#passwordNext",
                'button[jsname="LgbsSe"]',
                'div[id="passwordNext"]',
                'button[type="submit"]',
            ],
            5,
            mouse_pos=mouse_pos,
        )
    except Exception as btn_err:
        logger.warning(f"uc-login: password next button fallback ({btn_err})")
    logger.info("uc-login: password submitted")

    # Poll for URL transition away from password page
    for _wait_i in range(30):
        curr2 = driver.current_url
        if "challenge/pwd" not in curr2 and "signin/identifier" not in curr2:
            break
        time.sleep(0.5)
    uc_sleep(driver, 1.0, 2.0, mouse_pos=mouse_pos)


# ── Phase 3: 2FA / TOTP / reCAPTCHA / Challenge ──────────────────────────────
def _phase_2fa(
    driver: Any,
    email: str,
    password: str,
    totp_secret: str,
    mouse_pos: dict[str, int | float] | None = None,
) -> None:
    """Handle 2FA challenges including reCAPTCHA HITL, Authenticator selection, and TOTP."""
    import pyotp

    pos = mouse_pos if mouse_pos is not None else {"x": 100, "y": 100}
    curr2 = driver.current_url
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        body_text = ""

    is_challenge = (
        any(
            k in curr2
            for k in [
                "signin/challenge",
                "signin/v2/challenge",
                "/challenge/totp",
                "/challenge/selection",
                "/challenge/ipp",
                "/challenge/dp",
                "/challenge/az",
                "/challenge/sk",
                "/challenge/iap",
                "/challenge/iph",
                "/challenge/sl",
                "/challenge/dk",
                "2sv",
                "lookup",
            ]
        )
        or any(
            k in body_text
            for k in [
                "2-Step",
                "authenticator",
                "verification code",
                "Check your",
                "Try another way",
                "Verification",
                "2-step verification",
                "sent a notification",
            ]
        )
    ) and "challenge/pwd" not in curr2

    if not is_challenge:
        return

    logger.info(f"uc-login: challenge page detected ({curr2[:80]})")

    # ── reCAPTCHA -> HITL ─────────────────────────────────────────────────────
    if "challenge/recaptcha" in curr2:
        logger.info("uc-login: reCAPTCHA detected — pausing for HITL (human-in-the-loop)")
        try:
            import time as _rc_time
            import uuid as _rc_uuid

            xv_url = (
                f"{XIOSYNC_BASE}/api/v1/xioview/sessions/{NODE_NAME or 'uc-login'}/view"
                if XIOSYNC_BASE
                else "XIOVIEW: uc-login"
            )
            novnc_hint = f"\n⚡ noVNC (direct): {_NOVNC_URL}" if _NOVNC_URL else ""
            rc_notice = HITLNotice(
                organization_id=_rc_uuid.UUID("00000000-0000-7000-8000-000000000000"),
                session_id=NODE_NAME or "uc-login",
                challenge_type="recaptcha",
                message=(
                    f"🔒 reCAPTCHA — {email}\n"
                    f"👁 Solve via noVNC: {_NOVNC_URL or xv_url}{novnc_hint}\n"
                    f"URL: {curr2[:120]}\n"
                    "Tick 'I'm not a robot', complete any image challenge, then Resume."
                ),
                instructions=(
                    f"noVNC: {_NOVNC_URL or 'N/A'}. "
                    "Solve the reCAPTCHA — do NOT click 'Try another way'. "
                    "After checkbox clears, POST /hitl/{id}/resume."
                ),
            )
            _hitl_store.create(rc_notice)
            rc_notice_id = str(rc_notice.id)
            logger.info(
                f"uc-login: ✅ reCAPTCHA HITL id={rc_notice_id} — "
                f"noVNC={_NOVNC_URL or 'N/A'} — waiting up to 600s"
            )
            rc_deadline = _rc_time.time() + 600
            while _rc_time.time() < rc_deadline:
                _rc_time.sleep(2.0)
                rc_notice_state = _hitl_store._notices.get(rc_notice.id)
                if rc_notice_state and rc_notice_state.state == HITLState.RESUMED:
                    logger.info("uc-login: reCAPTCHA HITL resumed via manual signal ✅")
                    break
                try:
                    rc_curr = driver.current_url
                    still_recaptcha = any(
                        k in rc_curr for k in ("challenge/recaptcha", "challenge/az")
                    )
                    if not still_recaptcha:
                        logger.info(
                            f"uc-login: reCAPTCHA cleared (auto-detect) → {rc_curr[:70]} — auto-resuming HITL"
                        )
                        try:
                            _hitl_store._notices[rc_notice.id].state = HITLState.RESUMED
                        except Exception:
                            pass
                        break
                except Exception:
                    pass
            else:
                logger.warning("uc-login: reCAPTCHA HITL timed out (600s) — proceeding anyway")

            curr2 = driver.current_url
            logger.info(f"uc-login: post-reCAPTCHA URL: {curr2[:80]}")

            already_done = (
                "challenge/" not in curr2
                and "signin/" not in curr2
                and any(
                    k in curr2
                    for k in (
                        "mail.google.com",
                        "myaccount.google.com",
                        "accounts.google.com/b/",
                        "google.com/account",
                        "accounts.google.com/SignOutOptions",
                    )
                )
            )

            if already_done:
                logger.info(
                    "uc-login: ✅ already authenticated post-reCAPTCHA — skipping password/TOTP"
                )
            elif "challenge/pwd" in curr2 or "signin/v2/challenge/pwd" in curr2:
                logger.info("uc-login: password page post-reCAPTCHA — auto-entering password")
                try:
                    from selenium.webdriver.common.by import By as _By
                    from selenium.webdriver.common.keys import Keys as _Keys
                    from selenium.webdriver.support import expected_conditions as _EC
                    from selenium.webdriver.support.ui import WebDriverWait as _WDW

                    pw_sels_css = (
                        'input[type="password"], input[name="Passwd"], '
                        'input[name="password"], input[autocomplete="current-password"]'
                    )
                    pw_el = _WDW(driver, 12).until(
                        _EC.element_to_be_clickable((_By.CSS_SELECTOR, pw_sels_css))
                    )
                    pw_el.click()
                    uc_sleep(driver, 0.2, 0.4, mouse_pos=pos)
                    pw_el.clear()
                    pw_el.send_keys(password)
                    uc_sleep(driver, 0.3, 0.5, mouse_pos=pos)
                    try:
                        next_el = driver.find_element(
                            _By.CSS_SELECTOR,
                            '#passwordNext, button[jsname="LgbsSe"], '
                            'button[type="submit"], div[id="passwordNext"]',
                        )
                        next_el.click()
                    except Exception:
                        pw_el.send_keys(_Keys.RETURN)
                    uc_sleep(driver, 4.0, 6.0, mouse_pos=pos)
                    curr2 = driver.current_url
                    logger.info(f"uc-login: pwd post-reCAPTCHA submitted → {curr2[:80]}")
                except Exception as pwd_post_rc:
                    logger.warning(f"uc-login: post-reCAPTCHA pwd failed: {pwd_post_rc}")
            elif "challenge/recaptcha" in curr2 or "challenge/az" in curr2:
                logger.warning(f"uc-login: still on challenge after HITL: {curr2[:80]}")

            is_challenge = "challenge/" in curr2 and "challenge/pwd" not in curr2
            if not is_challenge:
                logger.info("uc-login: ✅ reCAPTCHA resolved — continuing login flow")
            else:
                logger.info(f"uc-login: challenge remaining: {curr2[:80]}")
        except Exception as rc_hitl_e:
            logger.warning(f"uc-login: reCAPTCHA HITL error: {rc_hitl_e}")

    # ── Step 1: "Try another way" ─────────────────────────────────────────────
    if (
        "/challenge/totp" not in curr2
        and "/challenge/selection" not in curr2
        and "/challenge/recaptcha" not in curr2
    ):
        logger.info("uc-login: polling for 'Try another way' button...")
        taw_result = False
        for taw_i in range(16):
            time.sleep(0.5)
            try:
                taw_btn = None
                try:
                    taw_btn = driver.find_element(
                        By.XPATH,
                        "//*[(self::button or @role='button' or self::a) and (contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'try another') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'more options'))]",
                    )
                except Exception:
                    pass

                if taw_btn and taw_btn.is_displayed():
                    choose_taw_rect_js = """
                    (function(){
                      var all=document.querySelectorAll('*');
                      for(var i=0;i<all.length;i++){
                        var t=(all[i].childElementCount===0?(all[i].innerText||all[i].textContent||''):'').toLowerCase().trim();
                        if(t.includes('try another')||t.includes('more options')){
                          var r = all[i].getBoundingClientRect();
                          return {x:r.x, y:r.y, w:r.width, h:r.height};
                        }
                      } return null;
                    })()
                    """
                    rect = cdp_eval(driver, choose_taw_rect_js)
                    if rect and rect.get("w", 0) > 0:
                        target_x = rect["x"] + rect["w"] * random.uniform(0.3, 0.7)
                        target_y = rect["y"] + rect["h"] * random.uniform(0.3, 0.7)
                        cdp_mouse_move(driver, pos["x"], pos["y"], target_x, target_y)
                        pos["x"], pos["y"] = target_x, target_y
                        time.sleep(random.uniform(0.05, 0.15))
                        driver.execute_cdp_cmd(
                            "Input.dispatchMouseEvent",
                            {
                                "type": "mousePressed",
                                "button": "left",
                                "clickCount": 1,
                                "x": int(target_x),
                                "y": int(target_y),
                            },
                        )
                        time.sleep(random.uniform(0.04, 0.12))
                        driver.execute_cdp_cmd(
                            "Input.dispatchMouseEvent",
                            {
                                "type": "mouseReleased",
                                "button": "left",
                                "clickCount": 1,
                                "x": int(target_x),
                                "y": int(target_y),
                            },
                        )
                        taw_result = True
                        logger.info(
                            f"uc-login: 'Try another way' clicked natively (attempt {taw_i + 1})"
                        )
                        break
            except Exception:
                pass

        if taw_result:
            uc_sleep(driver, 2.0, 3.0, mouse_pos=pos)
        else:
            logger.info("uc-login: 'Try another way' not found after 8s — TOTP may be direct")

    # ── Step 2: Select Authenticator ──────────────────────────────────────────
    sel_js = """
(function(){
  var dispatch=function(el){
    el.scrollIntoView({block:'center'});
    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(ev){
      el.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
    });
  };
  var t = document.querySelector('[data-challengetype="6"],[data-challengetype="12"],[data-challengetype="13"]');
  if(!t){
    var els=document.querySelectorAll('div[role="link"],div[role="button"],li,button,div[role="option"],div[role="listitem"],a');
    for(var i=0;i<els.length;i++){
      var txt=(els[i].innerText||els[i].textContent||'').toLowerCase();
      if(txt.includes('authenticator')||txt.includes('auth app')||txt.includes('google auth')||txt.includes('verification app')){
        t=els[i]; break;
      }
    }
  }
  if(t){dispatch(t);return 'selected';}
  var items=[];
  document.querySelectorAll('[data-challengetype]').forEach(function(e){items.push(e.getAttribute('data-challengetype')+':'+e.innerText.trim().slice(0,30));});
  return JSON.stringify({found:false, items:items, url:location.pathname});
})()"""
    sel_result = None
    for sel_i in range(16):
        time.sleep(0.5)
        sel_result = cdp_eval(driver, sel_js)
        if sel_result == "selected":
            logger.info(f"uc-login: authenticator selected (attempt {sel_i + 1})")
            break
        logger.info(f"uc-login: auth select poll [{sel_i}] → {str(sel_result)[:120]}")
    else:
        logger.warning("uc-login: authenticator not found after 8s")

    if sel_result == "selected":
        logger.info("uc-login: waiting for TOTP page after authenticator selection...")
        prev_url = driver.current_url
        for _wi in range(20):
            time.sleep(0.5)
            try:
                wu = driver.current_url
                if "/challenge/totp" in wu or "/challenge/ipp" in wu:
                    logger.info(f"uc-login: TOTP page ready ({wu[:60]})")
                    break
                if wu != prev_url and "/challenge/selection" not in wu:
                    logger.info(f"uc-login: navigated to ({wu[:60]})")
                    break
            except Exception:
                pass
        else:
            logger.warning("uc-login: TOTP page did not appear after 10s")
    else:
        logger.warning("uc-login: skipping TOTP fill — authenticator was not selected")

    # ── Step 3: Wait for TOTP input to render in DOM ──────────────────────────
    logger.info("uc-login: polling for TOTP input element in DOM...")
    totp_input_found = False
    for inp_i in range(14):
        time.sleep(0.5)
        try:
            ic = driver.execute_cdp_cmd(
                "Runtime.evaluate",
                {
                    "expression": """
(function(){
  var all=Array.from(document.querySelectorAll('input:not([type="hidden"])'));
  var vis=all.filter(function(i){var r=i.getBoundingClientRect();return r.width>0&&r.height>0;});
  return JSON.stringify({total:all.length,visible:vis.length,
    names:vis.slice(0,4).map(function(i){return i.name+'|'+i.type+'|'+i.id;})});
})()""",
                    "returnByValue": True,
                    "timeout": 2000,
                },
            )
            import json as _jdom

            ii = _jdom.loads(ic.get("result", {}).get("value", '{"visible":0}'))
            logger.info(f"uc-login: TOTP DOM poll [{inp_i}]: {ii}")
            if ii.get("visible", 0) > 0:
                totp_input_found = True
                time.sleep(0.3)
                break
        except Exception as pe:
            logger.warning(f"uc-login: TOTP DOM poll error: {pe}")
    if not totp_input_found:
        logger.warning("uc-login: TOTP input not visible after 7s — attempting fill anyway")

    # ── Step 4: TOTP entry ────────────────────────────────────────────────────
    curr3 = driver.current_url
    on_totp_page = (
        is_challenge
        and "/challenge/selection" not in curr3
        and "/challenge/dp" not in curr3
        and "/challenge/az" not in curr3
        and "/challenge/ipp" not in curr3
        and "/challenge/" in curr3
    )
    if is_challenge and not on_totp_page:
        logger.info(f"uc-login: not on TOTP page ({curr3[:70]}) — skipping TOTP fill")
    if on_totp_page and totp_secret:
        try:
            totp_min_remaining = int(os.environ.get("XIOSYNC_TOTP_MIN_REMAINING_SEC", "5"))
            totp_remaining = 30 - (time.time() % 30)
            if totp_remaining < totp_min_remaining:
                logger.info(
                    f"TOTP window has {totp_remaining:.1f}s left, waiting for fresh window..."
                )
                time.sleep(totp_remaining + 0.5)
            totp_code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
            logger.info(
                f"TOTP generated with {30 - (time.time() % 30):.1f}s remaining in window"
            )
            logger.info(f"uc-login: TOTP code=****{totp_code[-2:]} url={curr3[:60]}")

            totp_rect_js = """
            (function() {
              var all = Array.from(document.querySelectorAll('input:not([type="hidden"])'));
              var inp = null;
              for (var i=0;i<all.length;i++) {
                var r = all[i].getBoundingClientRect();
                if (r.width>0 && r.height>0) {
                  var a = (all[i].id+' '+all[i].name+' '+(all[i].getAttribute('aria-label')||'')+' '+(all[i].placeholder||'')).toLowerCase();
                  if (a.includes('code')||a.includes('totp')||a.includes('pin')||all[i].type==='tel'||all[i].type==='number') {
                    inp = all[i]; break;
                  }
                }
              }
              if (!inp) inp = all.find(function(i){var r=i.getBoundingClientRect();return r.width>0&&r.height>0&&['tel','number','text'].includes(i.type);});
              if (!inp && location.pathname.includes('/challenge/totp')) {
                inp = all.find(function(i){var r=i.getBoundingClientRect();return r.width>0&&r.height>0&&(i.type==='password'||i.name==='Passwd');});
              }
              if (inp) {
                  var r = inp.getBoundingClientRect();
                  return {x:r.x, y:r.y, w:r.width, h:r.height};
              }
              return null;
            })()"""

            rect = cdp_eval(driver, totp_rect_js)
            if rect and rect.get("w", 0) > 0:
                target_x = rect["x"] + rect["w"] * random.uniform(0.3, 0.7)
                target_y = rect["y"] + rect["h"] * random.uniform(0.3, 0.7)
                cdp_mouse_move(driver, pos["x"], pos["y"], target_x, target_y)
                pos["x"], pos["y"] = target_x, target_y
                time.sleep(random.uniform(0.05, 0.15))
                driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mousePressed",
                        "button": "left",
                        "clickCount": 1,
                        "x": int(target_x),
                        "y": int(target_y),
                    },
                )
                time.sleep(random.uniform(0.04, 0.12))
                driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseReleased",
                        "button": "left",
                        "clickCount": 1,
                        "x": int(target_x),
                        "y": int(target_y),
                    },
                )

                uc_sleep(driver, 0.1, 0.3, mouse_pos=pos)
                cdp_clear_input(driver)
                uc_sleep(driver, 0.1, 0.3, mouse_pos=pos)
                cdp_type_text(driver, totp_code)
                logger.info("uc-login: CDP TOTP natively typed")

                time.sleep(0.3)

                totp_btn_js = """
                (function(){
                  var nb=document.querySelector('#totpNext,#idvPreregisteredPhoneNext,[jsname="LgbsSe"],button[type="submit"],button[aria-label*="Next"],button[aria-label*="Verify"]');
                  if(nb) {
                      var r = nb.getBoundingClientRect();
                      return {x:r.x, y:r.y, w:r.width, h:r.height};
                  }
                  return null;
                })()"""
                btn_rect = cdp_eval(driver, totp_btn_js)
                if btn_rect and btn_rect.get("w", 0) > 0:
                    target_x = btn_rect["x"] + btn_rect["w"] * random.uniform(0.3, 0.7)
                    target_y = btn_rect["y"] + btn_rect["h"] * random.uniform(0.3, 0.7)
                    cdp_mouse_move(driver, pos["x"], pos["y"], target_x, target_y)
                    pos["x"], pos["y"] = target_x, target_y
                    time.sleep(random.uniform(0.05, 0.15))
                    driver.execute_cdp_cmd(
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mousePressed",
                            "button": "left",
                            "clickCount": 1,
                            "x": int(target_x),
                            "y": int(target_y),
                        },
                    )
                    time.sleep(random.uniform(0.04, 0.12))
                    driver.execute_cdp_cmd(
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mouseReleased",
                            "button": "left",
                            "clickCount": 1,
                            "x": int(target_x),
                            "y": int(target_y),
                        },
                    )
                    logger.info("uc-login: TOTP Next clicked natively")
                else:
                    driver.execute_cdp_cmd(
                        "Input.dispatchKeyEvent",
                        {"type": "keyDown", "windowsVirtualKeyCode": 13, "key": "Enter"},
                    )
                    time.sleep(0.05)
                    driver.execute_cdp_cmd(
                        "Input.dispatchKeyEvent",
                        {"type": "keyUp", "windowsVirtualKeyCode": 13, "key": "Enter"},
                    )
                    logger.info("uc-login: TOTP Next Enter pressed natively")

                # Wait for post-TOTP redirect chain
                for _pw in range(24):
                    time.sleep(0.5)
                    try:
                        pw_url = driver.current_url
                        if "/challenge/" not in pw_url:
                            logger.info(f"uc-login: left challenge → {pw_url[:70]}")
                            break
                    except Exception:
                        break
                time.sleep(2.0)

                # Detect and recover from transient proxy error page
                try:
                    err_url = driver.current_url
                    page_src = ""
                    try:
                        page_src = driver.execute_script(
                            "return document.body?.innerText || ''"
                        )[:200]
                    except Exception:
                        pass
                    is_err_page = (
                        err_url.startswith("data:")
                        or "chrome-error://" in err_url
                        or any(
                            k in page_src.lower()
                            for k in (
                                "err_network",
                                "err_proxy",
                                "net::err",
                                "this page isn't working",
                            )
                        )
                    )
                    if is_err_page:
                        logger.warning(
                            f"uc-login: network error page detected ({err_url[:60]}) — retrying navigation"
                        )
                        safe_get(driver, "https://mail.google.com/mail/u/0/#inbox", wait=3.0, mouse_pos=pos)
                        time.sleep(2.0)
                except Exception as erp:
                    logger.warning(f"uc-login: error-page check failed: {erp}")
            else:
                logger.warning("uc-login: CDP TOTP fill failed — could not find input")
        except Exception as te:
            logger.warning(f"uc-login: TOTP block failed: {te}")


# ── Phase 4: Interstitials & Post-Login Prompts ──────────────────────────────
def _phase_interstitials(
    driver: Any,
    mouse_pos: dict[str, int | float] | None = None,
) -> None:
    """Handle post-login interstitials (recovery, phone, photo, home address, stay signed in)."""
    pos = mouse_pos if mouse_pos is not None else {"x": 100, "y": 100}

    # "Stay signed in?" prompt
    try:
        cdp_click_element(
            driver,
            ["#confirm-button", '[data-action="confirm"]', 'button[jsname="LgbsSe"]'],
            timeout=2,
            mouse_pos=pos,
        )
        logger.info("uc-login: 'Stay signed in' confirmed natively")
        time.sleep(1)
    except Exception:
        pass

    for pl_i in range(8):
        try:
            # reCAPTCHA check in post-login
            try:
                if (
                    "challenge/recaptcha" in driver.current_url
                    or "recaptcha" in driver.page_source.lower()[:5000]
                ):
                    rc_path = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "recaptcha_solver.py",
                    )
                    if os.path.exists(rc_path):
                        import importlib.util as _rc_ilu2

                        rc_spec2 = _rc_ilu2.spec_from_file_location("recaptcha_solver", rc_path)
                        if rc_spec2 and rc_spec2.loader:
                            rc_mod2 = _rc_ilu2.module_from_spec(rc_spec2)
                            rc_spec2.loader.exec_module(rc_mod2)
                            if rc_mod2.solve_recaptcha(
                                driver,
                                log_fn=lambda m: logger.info(m),
                                sleep_fn=lambda a=0.8, b=2.0: uc_sleep(driver, a, b, mouse_pos=pos),
                            ):
                                logger.info("uc-login: reCAPTCHA in post-login loop solved")
                                uc_sleep(driver, 2.0, 3.0, mouse_pos=pos)
                                continue
            except Exception as rcpl:
                logger.warning(f"uc-login: post-login reCAPTCHA check error: {rcpl}")

            uc_sleep(driver, 1.0, 2.0, mouse_pos=pos)
            cur_pl_url = ""
            try:
                cur_pl_url = driver.current_url
            except Exception:
                pass

            is_interstitial_url = any(
                u in cur_pl_url
                for u in (
                    "gds.google.com",
                    "myaccount.google.com/intro",
                    "myaccount.google.com/profile/photo",
                    "accounts.google.com/b/0/EditProfile",
                    "photos.google.com",
                )
            )

            ps = driver.page_source.lower()
            prompt_keywords = [
                "recovery",
                "make sure you can always sign in",
                "protect your account",
                "passkey",
                "add a phone number",
                "not now",
                "home address",
                "set a home address",
                "add a profile photo",
                "choose a photo",
                "set your photo",
                "profile photo",
                "profile picture",
                "add photo",
                "choose photo",
                "upload a photo",
                "upload photo",
                "select photo",
                "take photo",
                "keep your account secure",
                "add recovery",
                "confirm your recovery",
                "update your info",
                "verify your recovery",
                "add backup",
            ]
            if is_interstitial_url or any(x in ps for x in prompt_keywords):
                logger.info(
                    f"uc-login: post-login prompt detected (iter {pl_i + 1}, url={cur_pl_url[:60]}) — clicking Skip/Cancel"
                )
                try:
                    skip_rect_js = """
                    (function(){
                      var skip_texts = ['skip','not now','cancel','no thanks','close','later','maybe later','dismiss'];
                      var all=document.querySelectorAll('button,a[role="button"],div[role="button"]');
                      for(var i=0;i<all.length;i++){
                        var t=(all[i].innerText||all[i].textContent||all[i].getAttribute('aria-label')||'').toLowerCase().trim();
                        for(var j=0;j<skip_texts.length;j++){
                          if(t===skip_texts[j]||t.includes(skip_texts[j])){
                            var r = all[i].getBoundingClientRect();
                            if(r.width>0&&r.height>0) return {x:r.x, y:r.y, w:r.width, h:r.height};
                          }
                        }
                      }
                      var closes=document.querySelectorAll('button[aria-label="Close"],button[aria-label="close"],button[data-dismiss]');
                      for(var i=0;i<closes.length;i++){
                        var r=closes[i].getBoundingClientRect();
                        if(r.width>0&&r.height>0) return {x:r.x,y:r.y,w:r.width,h:r.height};
                      }
                      return null;
                    })()
                    """
                    rect = cdp_eval(driver, skip_rect_js)
                    if rect and rect.get("w", 0) > 0:
                        target_x = rect["x"] + rect["w"] * random.uniform(0.3, 0.7)
                        target_y = rect["y"] + rect["h"] * random.uniform(0.3, 0.7)
                        cdp_mouse_move(driver, pos["x"], pos["y"], target_x, target_y)
                        pos["x"], pos["y"] = target_x, target_y
                        time.sleep(random.uniform(0.05, 0.15))
                        driver.execute_cdp_cmd(
                            "Input.dispatchMouseEvent",
                            {
                                "type": "mousePressed",
                                "button": "left",
                                "clickCount": 1,
                                "x": int(target_x),
                                "y": int(target_y),
                            },
                        )
                        time.sleep(random.uniform(0.04, 0.12))
                        driver.execute_cdp_cmd(
                            "Input.dispatchMouseEvent",
                            {
                                "type": "mouseReleased",
                                "button": "left",
                                "clickCount": 1,
                                "x": int(target_x),
                                "y": int(target_y),
                            },
                        )
                        logger.info(
                            f"uc-login: post-login prompt dismissed (Skip/Cancel) natively (iter {pl_i + 1})"
                        )
                        uc_sleep(driver, 2.0, 3.0, mouse_pos=pos)
                    else:
                        # Fallback: Escape key
                        try:
                            driver.execute_cdp_cmd(
                                "Input.dispatchKeyEvent",
                                {
                                    "type": "keyDown",
                                    "windowsVirtualKeyCode": 27,
                                    "key": "Escape",
                                },
                            )
                            time.sleep(0.05)
                            driver.execute_cdp_cmd(
                                "Input.dispatchKeyEvent",
                                {"type": "keyUp", "windowsVirtualKeyCode": 27, "key": "Escape"},
                            )
                            logger.info(
                                f"uc-login: post-login prompt — Escape sent (iter {pl_i + 1})"
                            )
                            uc_sleep(driver, 1.5, 2.5, mouse_pos=pos)
                        except Exception:
                            break
                except Exception as ske:
                    logger.warning(f"uc-login: post-login Skip/Cancel failed: {ske}")
                    break
            else:
                logger.info(
                    f"uc-login: post-login prompts cleared (iter {pl_i + 1}, url={cur_pl_url[:60]})"
                )
                break
        except Exception as ple:
            logger.warning(f"uc-login: post-login prompt check failed: {ple}")
            break

    # If on an interstitial instead of Gmail, navigate to Gmail now
    try:
        post_dismiss_url = driver.current_url
        if (
            "mail.google.com" not in post_dismiss_url
            and "accounts.google.com" not in post_dismiss_url
        ):
            logger.info(
                f"uc-login: navigating to Gmail after prompts (was on {post_dismiss_url[:60]})"
            )
            safe_get(driver, "https://mail.google.com/mail/u/0/#inbox", wait=3.0, mouse_pos=pos)
            time.sleep(1.5)
    except Exception as gm_nav:
        logger.warning(f"uc-login: Gmail nav after prompts failed: {gm_nav}")

    # Settle wait
    for _settle_i in range(10):
        try:
            settle_url = driver.current_url
            if "accounts.google.com" not in settle_url or (
                "signin" not in settle_url and "challenge" not in settle_url
            ):
                break
        except Exception:
            break
        time.sleep(0.8)


# ── Phase 5: Verification & Cookie Capture ────────────────────────────────────
def _phase_cookie_capture(
    driver: Any,
    user_data_dir: str | None = None,
    mouse_pos: dict[str, int | float] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify session, capture cookies via Network.getAllCookies, and quit driver to free RAM."""
    pos = mouse_pos if mouse_pos is not None else {"x": 100, "y": 100}
    meta = meta or {}

    final_url_pre_verify = driver.current_url
    logger.info(f"uc-login: pre-verify URL: {final_url_pre_verify[:100]}")
    if "accounts.google.com" in final_url_pre_verify and (
        "signin" in final_url_pre_verify or "challenge" in final_url_pre_verify
    ):
        return {
            "ok": False,
            "error": f"Login failed — stuck on sign-in/challenge page: {final_url_pre_verify[:120]}",
        }

    verify_url = ""
    try:
        safe_get(driver, "https://myaccount.google.com/", wait=3.0, mouse_pos=pos)
        time.sleep(1.5)
        verify_url = driver.current_url

        verify_err = False
        try:
            v_src = driver.execute_script("return document.body?.innerText || ''")[:300]
            verify_err = (
                verify_url.startswith("data:")
                or "chrome-error://" in verify_url
                or any(
                    k in v_src.lower()
                    for k in (
                        "err_network",
                        "err_socks",
                        "err_proxy",
                        "net::err",
                        "this page isn't working",
                        "this site can't be reached",
                    )
                )
            )
        except Exception:
            pass

        if verify_err:
            logger.warning(
                f"uc-login: proxy error on myaccount navigate ({verify_url[:60]}) — trying google.com fallback"
            )
            try:
                safe_get(driver, "https://www.google.com/", wait=3.0, mouse_pos=pos)
                time.sleep(1.5)
                verify_url = driver.current_url
                logger.info(f"uc-login: google.com fallback verify URL: {verify_url[:80]}")
            except Exception as gfb_e:
                logger.warning(
                    f"uc-login: google.com fallback also failed ({gfb_e}) — proceeding with cookie capture"
                )
    except Exception as nav_e:
        logger.warning(
            f"uc-login: myaccount navigate failed ({nav_e}) — proceeding with cookie capture"
        )
        try:
            verify_url = driver.current_url
        except Exception:
            verify_url = ""

    final_url = verify_url or "https://accounts.google.com/"
    logger.info(f"uc-login: final verify URL: {final_url[:100]}")

    if (
        "accounts.google.com/v3/signin" in final_url
        or "accounts.google.com/ServiceLogin" in final_url
        or ("/account/about/" in final_url and "myaccount.google.com" not in final_url)
    ):
        return {
            "ok": False,
            "error": f"Login failed — sign-in page or unauthenticated: {final_url[:120]}",
        }

    try:
        all_data = driver.execute_cdp_cmd("Network.getAllCookies", {})
        cookies = all_data.get("cookies", [])
        domains: dict[str, int] = {}
        for c in cookies:
            d = c.get("domain", "?")
            domains[d] = domains.get(d, 0) + 1
        logger.info(f"uc-login: cookies={len(cookies)} by domain: {domains}")
    except Exception as ce:
        logger.warning(
            f"uc-login: Network.getAllCookies failed ({ce}), falling back to get_cookies()"
        )
        cookies = driver.get_cookies()

    try:
        driver.get("about:blank")
        time.sleep(1.0)
    except Exception:
        pass

    try:
        driver.quit()
        logger.info("uc-login: Chrome quit (RAM freed)")
    except Exception as qe:
        logger.debug(f"uc-login: Chrome quit error (non-fatal): {qe}")

    logger.info(f"uc-login: success cookies={len(cookies)} final_url={final_url}")
    return {
        "ok": True,
        "cookies": cookies,
        "engine": "uc",
        "final_url": final_url,
        "profile_dir": user_data_dir,
        "uc_port": 0,
        "canvas_seed": meta.get("canvas_seed", 0),
        "audio_seed": meta.get("audio_seed", 0),
        "webgl_seed": 0,
        "ua_string": meta.get("ua_string", ""),
        "timezone": meta.get("timezone", "UTC"),
        "locale": meta.get("locale", "en-US"),
    }


# ── Main Entry Point ──────────────────────────────────────────────────────────
def _run_uc_login_sync(
    driver: Any = None,
    session_info: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    *,
    email: str | None = None,
    password: str | None = None,
    totp_secret: str | None = None,
    proxy_url: str | None = None,
    user_agent: str | None = None,
    user_data_dir: str | None = None,
    pre_launched_driver: Any = None,
    exit_node_public_ip: str | None = None,
    fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute synchronous UC stealth Google login flow.

    Supports both decomposed style:
        _run_uc_login_sync(driver, session_info, options)
    And direct monolithic signature with individual kwargs:
        _run_uc_login_sync(email=..., password=..., pre_launched_driver=...)
    """
    session_info = session_info or {}
    options = options or {}

    email_val = email or session_info.get("email") or ""
    password_val = password or session_info.get("password") or ""
    totp_secret_val = totp_secret or session_info.get("totp_secret") or ""
    proxy_url_val = (
        proxy_url
        or options.get("proxy_url")
        or session_info.get("proxy_url")
    )
    user_agent_val = (
        user_agent
        or options.get("user_agent")
        or session_info.get("user_agent")
        or ""
    )
    user_data_dir_val = (
        user_data_dir
        or options.get("user_data_dir")
        or session_info.get("user_data_dir")
    )
    exit_node_public_ip_val = (
        exit_node_public_ip
        or options.get("exit_node_public_ip")
        or session_info.get("exit_node_public_ip")
    )
    fingerprint_val = (
        fingerprint
        or options.get("fingerprint")
        or session_info.get("fingerprint")
        or {}
    )

    drv = pre_launched_driver or driver
    uc_debug_port = 0

    if drv is not None:
        logger.info("uc-login: reusing pre-launched UC Chrome driver (2-step flow)")
        try:
            uc_debug_port = next(
                int(a.split("=")[1])
                for a in drv.options.arguments
                if a.startswith("--remote-debugging-port=")
            )
        except Exception:
            uc_debug_port = 0
        logger.info(f"uc-login: pre-launched driver on port {uc_debug_port}")
        try:
            drv.set_page_load_timeout(30)
            drv.set_script_timeout(15)
        except Exception:
            pass
    else:
        # Launch fresh UC Chrome instance
        pdir = user_data_dir_val or "/tmp/uc_login_anon"
        drv, uc_debug_port = _launch_uc_chrome_for_login(
            profile_dir=pdir,
            proxy_url=proxy_url_val,
            fingerprint=fingerprint_val,
        )
        try:
            drv.set_page_load_timeout(45)
            drv.set_script_timeout(15)
        except Exception:
            pass

    mouse_pos: dict[str, int | float] = {"x": 100, "y": 100}

    # Resolve geo & timezone
    uc_geo = _resolve_ip_geo(exit_node_public_ip_val or None)
    uc_timezone = uc_geo.get("timezone", "America/New_York")
    uc_locale = uc_geo.get("locale", "en-US")
    uc_lat = uc_geo.get("lat", 40.7128)
    uc_lon = uc_geo.get("lon", -74.0060)

    try:
        # ── CDP-level device emulation ────────────────────────────────────────
        try:
            drv.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": uc_timezone})
            logger.info(f"uc-login: CDP timezone={uc_timezone}")
        except Exception as tz_e:
            logger.warning(f"uc-login: CDP timezone override failed: {tz_e}")

        try:
            drv.execute_cdp_cmd(
                "Emulation.setGeolocationOverride",
                {"latitude": uc_lat, "longitude": uc_lon, "accuracy": 1000},
            )
            logger.info(f"uc-login: CDP geolocation lat={uc_lat:.2f} lon={uc_lon:.2f}")
        except Exception as geo_e:
            logger.warning(f"uc-login: CDP geolocation override failed: {geo_e}")

        fp_ua = fingerprint_val or {}
        ch_platform = fp_ua.get("ch_platform", "Linux")
        ch_arch = fp_ua.get("ch_arch", "x86")
        is_mobile = bool(fp_ua.get("is_mobile", False))
        ua_str = fp_ua.get("ua_template", user_agent_val)

        try:
            drv.execute_cdp_cmd(
                "Network.setUserAgentOverride",
                {
                    "userAgent": ua_str,
                    "acceptLanguage": f"{uc_locale},{uc_locale.split('-')[0]};q=0.9,en;q=0.8",
                    "platform": ch_platform,
                    "userAgentMetadata": {
                        "brands": [
                            {"brand": "Google Chrome", "version": "131"},
                            {"brand": "Chromium", "version": "131"},
                            {"brand": "Not_A Brand", "version": "24"},
                        ],
                        "fullVersion": _CHROME_VERSION,
                        "platform": ch_platform,
                        "platformVersion": "10.0.0",
                        "architecture": ch_arch,
                        "model": "",
                        "mobile": is_mobile,
                        "bitness": "64",
                        "wow64": False,
                    },
                },
            )
            logger.info(f"uc-login: CDP UA+Accept-Language={uc_locale} set")
        except Exception as ua_e:
            logger.warning(f"uc-login: CDP UA override failed: {ua_e}")

        # Inject stealth JS
        try:
            import datetime as _dtm
            import zoneinfo as _zi2

            try:
                tz_obj2 = _zi2.ZoneInfo(uc_timezone)
                tz_off2 = -int(_dtm.datetime.now(tz_obj2).utcoffset().total_seconds() // 60)
            except Exception:
                tz_off2 = 0

            uc_canvas_seed = fingerprint_val.get("canvas_seed", random.randint(0x1000, 0xFFFF))
            uc_audio_seed = fingerprint_val.get("audio_seed", random.randint(0x1000, 0xFFFF))
            cores = str(fingerprint_val.get("cores", os.cpu_count() or 2))
            ram = str(fingerprint_val.get("ram", max(2, min(8, (os.cpu_count() or 2) * 2))))
            platform = fingerprint_val.get("platform", "Linux x86_64")
            screen_w = str(fingerprint_val.get("width", 1920))
            screen_h = str(fingerprint_val.get("height", 1080))
            screen_ah = str(max(100, int(screen_h) - 40))
            webgl_v = "Google Inc. (NVIDIA)"
            webgl_r = fingerprint_val.get(
                "webgl_renderer",
                "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            )
            if "Apple" in webgl_r:
                webgl_v = "Apple"
            elif "Intel" in webgl_r:
                webgl_v = "Google Inc. (Intel)"
            elif "AMD" in webgl_r:
                webgl_v = "Google Inc. (AMD)"

            uc_stealth = (
                _STEALTH_JS.replace("'{TIMEZONE}'", f"'{uc_timezone}'")
                .replace("{TIMEZONE}", uc_timezone)
                .replace("'{LOCALE}'", f"'{uc_locale}'")
                .replace("{LOCALE}", uc_locale)
                .replace("{CANVAS_SEED}", str(uc_canvas_seed))
                .replace("{AUDIO_SEED}", str(uc_audio_seed))
                .replace("{LAT}", str(uc_lat))
                .replace("{LON}", str(uc_lon))
                .replace("'{UA}'", f"'{ua_str}'")
                .replace("{UA}", ua_str)
                .replace("{TZ_OFFSET}", str(tz_off2))
                .replace("{CORES}", cores)
                .replace("{RAM}", ram)
                .replace("{PLATFORM}", platform)
                .replace("{CH_PLATFORM}", ch_platform)
                .replace("{CH_ARCH}", ch_arch)
                .replace("{IS_MOBILE}", "true" if is_mobile else "false")
                .replace("{SCREEN_W}", screen_w)
                .replace("{SCREEN_H}", screen_h)
                .replace("{SCREEN_AH}", screen_ah)
                .replace("{WEBGL_V}", webgl_v)
                .replace("{WEBGL_R}", webgl_r)
            )
            drv.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": uc_stealth},
            )
            cdp_eval(drv, uc_stealth)
            logger.info(
                f"uc-login: ✅ stealth JS injected — "
                f"tz={uc_timezone} locale={uc_locale} canvas_seed={uc_canvas_seed}"
            )
        except Exception as sj_e:
            logger.warning(f"uc-login: stealth JS injection failed: {sj_e}")

        # Set Accept-Language HTTP header
        try:
            accept_lang = f"{uc_locale},{uc_locale.split('-')[0]};q=0.9,en;q=0.8"
            drv.execute_cdp_cmd(
                "Network.setExtraHTTPHeaders",
                {"headers": {"Accept-Language": accept_lang}},
            )
            drv.execute_cdp_cmd("Network.enable", {})
            logger.info(f"uc-login: HTTP Accept-Language header set → {accept_lang}")
        except Exception as ah_e:
            logger.warning(f"uc-login: setExtraHTTPHeaders failed: {ah_e}")

        # ── Execution Pipeline ────────────────────────────────────────────────
        # Phase 0: Pre-auth check
        preauth = _phase_preauth_check(
            drv,
            email_val,
            user_data_dir=user_data_dir_val,
            uc_debug_port=uc_debug_port,
            mouse_pos=mouse_pos,
        )
        if preauth:
            return preauth

        # ServiceLogin navigation & chooser
        already_authed = _phase_service_login_nav(drv, email_val, mouse_pos=mouse_pos)

        if not already_authed:
            # Phase 1: Identifier
            id_res = _phase_identifier(drv, email_val, mouse_pos=mouse_pos)
            if id_res:
                return id_res

            # Phase 2: Password
            _phase_password(drv, password_val, mouse_pos=mouse_pos)

            # Phase 3: 2FA & Challenges
            _phase_2fa(
                drv,
                email_val,
                password_val,
                totp_secret_val,
                mouse_pos=mouse_pos,
            )

        # Phase 4: Interstitials
        _phase_interstitials(drv, mouse_pos=mouse_pos)

        # Phase 5: Verification & Cookie Capture
        meta = {
            "canvas_seed": uc_canvas_seed if "uc_canvas_seed" in locals() else 0,
            "audio_seed": uc_audio_seed if "uc_audio_seed" in locals() else 0,
            "ua_string": ua_str,
            "timezone": uc_timezone,
            "locale": uc_locale,
        }
        return _phase_cookie_capture(
            drv,
            user_data_dir=user_data_dir_val,
            mouse_pos=mouse_pos,
            meta=meta,
        )

    except Exception as exc:
        logger.error(f"uc-login exception: {exc}")
        if drv is not None:
            try:
                drv.quit()
            except Exception:
                pass
        return {"ok": False, "error": str(exc)}
