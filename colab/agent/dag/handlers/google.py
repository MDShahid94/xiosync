"""Google-specific post-action handlers for DAG execution.

Handles Google 2SV challenge pages and post-login interstitials.
"""

from __future__ import annotations

import os
from typing import Any

try:
    from colab.agent.display.novnc import get_novnc_url
except ImportError:
    try:
        from agent.display.novnc import get_novnc_url
    except ImportError:
        def get_novnc_url() -> str | None:
            return None


# ── Domain-Agnostic Post-Action Handler Registry ─────────────────────────────
# Instead of hardcoding domain-specific logic (e.g. Google challenge pages)
# inside the generic DAG runner, handlers are registered per (domain, intent)
# pair and dispatched dynamically after each step completes.
#
# Handler signature:
#   async def handler(page, vars_dict, xiosync_base, hdrs, run_id, logger) -> None
#
# Register with:
#   _DAG_POST_ACTION_HANDLERS[("accounts.google.com", "click_signin")] = [handler1, handler2]
#
# The DAG runner calls all registered handlers after a successful step.
_DAG_POST_ACTION_HANDLERS: dict[tuple[str, str], list] = {}


async def _google_challenge_handler(page, vars_dict, xiosync_base, hdrs, run_id, logger):
    """Handle all Google 2SV challenge pages after password entry.
    Mirrors UC login challenge logic: reCAPTCHA→HITL, dp→Try another way,
    selection→authenticator, TOTP page→fill+submit (window-aware).
    """
    import asyncio as _ac
    import time as _tm

    try:
        import pyotp as _pt
    except ImportError:
        _pt = None

    _novnc_base = get_novnc_url() or os.environ.get("NOVNC_URL", "http://localhost:6080/vnc.html")
    _NOVNC = (_novnc_base or "http://localhost:6080/vnc.html") + "?autoconnect=true&resize=scale"

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass

    curr_url = page.url
    logger.info(f"dag_challenge: post-signin URL = {curr_url[:80]}")

    # ── reCAPTCHA → HITL ──────────────────────────────────────────────────
    if "challenge/recaptcha" in curr_url or "challenge/az" in curr_url:
        logger.info("dag_challenge: reCAPTCHA detected — triggering HITL")
        try:
            import httpx as _hx

            _msg = (
                f"reCAPTCHA for run {str(run_id)[:8]} — "
                f"solve live: {_NOVNC} — URL: {curr_url[:120]}"
            )
            async with _hx.AsyncClient(timeout=10) as _cl:
                await _cl.post(
                    f"{xiosync_base}/api/v1/xioflow/events/runs-internal/{run_id}/hitl-pause",
                    headers=hdrs,
                    json={
                        "challenge_type": "recaptcha",
                        "novnc_url": _NOVNC,
                        "message": _msg,
                        "current_url": curr_url,
                    },
                )
            logger.info("dag_challenge: HITL notice sent — polling up to 300s for resume")
            for _hw in range(60):
                await _ac.sleep(5)
                try:
                    async with _hx.AsyncClient(timeout=10) as _cl:
                        _sr = await _cl.get(
                            f"{xiosync_base}/api/v1/xioflow/events/runs-internal/{run_id}/hitl-status",
                            headers=hdrs,
                        )
                    if _sr.json().get("hitl_resumed"):
                        logger.info("dag_challenge: HITL resumed — continuing")
                        break
                except Exception:
                    pass
            else:
                logger.warning("dag_challenge: HITL timed out 300s — proceeding anyway")
            curr_url = page.url
        except Exception as _he:
            logger.warning(f"dag_challenge: HITL error: {_he}")

    # ── Challenge navigation loop: TAW → select authenticator → TOTP ─────
    # Google's 2SV chain can be multi-hop: pwd→dp→selection→totp
    # We loop up to 4 rounds, each round doing TAW then authenticator-select,
    # until we land on /challenge/totp or /challenge/selection.
    try:
        _title = await page.title()
    except Exception:
        _title = ""
    _is_challenge = any(
        k in curr_url
        for k in [
            "signin/challenge",
            "/challenge/dp",
            "/challenge/pwd",
            "/challenge/sk",
            "/challenge/ipp",
            "/challenge/iap",
        ]
    ) or any(k in _title for k in ["2-Step", "Verification", "Verify it", "Check your"])

    _taw_js = """(function(){
      var all=document.querySelectorAll('*');
      for(var i=0;i<all.length;i++){
        var t=(all[i].childElementCount===0?(all[i].innerText||all[i].textContent||''):'').toLowerCase().trim();
        if(t.includes('try another')||t.includes('more options')){all[i].click();return 'clicked';}
      } return 'not_found';
    })()"""
    _sel_js = """(function(){
      var d=function(el){el.scrollIntoView({block:'center'});
        ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(e){
          el.dispatchEvent(new MouseEvent(e,{bubbles:true,cancelable:true,view:window}));});};
      var t=document.querySelector('[data-challengetype="6"],[data-challengetype="12"],[data-challengetype="13"]');
      if(!t){var els=document.querySelectorAll('div[role="link"],div[role="button"],li,button,a');
        for(var i=0;i<els.length;i++){var txt=(els[i].innerText||els[i].textContent||'').toLowerCase();
          if(txt.includes('authenticator')||txt.includes('auth app')||txt.includes('google auth')
             ||txt.includes('verification app')){t=els[i];break;}}}
      if(t){d(t);return 'selected';} return 'not_found';
    })()"""

    if _is_challenge:
        for _round in range(4):  # up to 4 TAW rounds (pwd→dp→selection→totp)
            curr_url = page.url
            logger.info(f"dag_challenge: round {_round + 1} — URL={curr_url[:70]}")
            if "/challenge/totp" in curr_url or "/challenge/ipp" in curr_url:
                break  # reached TOTP input page — stop navigating

            # ── Step A: click 'Try another way' if not on selection page ──
            if "/challenge/selection" not in curr_url:
                logger.info(f"dag_challenge: polling 'Try another way' (round {_round + 1})")
                for _ti in range(16):  # up to 8s
                    await _ac.sleep(0.5)
                    try:
                        if await page.evaluate(_taw_js) == "clicked":
                            logger.info(
                                f"dag_challenge: TAW clicked (round {_round + 1}, attempt {_ti + 1})"
                            )
                            await _ac.sleep(2.5)
                            curr_url = page.url
                            break
                    except Exception:
                        pass
                if "/challenge/totp" in curr_url or "/challenge/ipp" in curr_url:
                    break  # TAW led directly to TOTP

            # ── Step B: select Authenticator from selection/dp menu ────────
            logger.info(f"dag_challenge: trying authenticator select (round {_round + 1})")
            for _si in range(16):  # up to 8s
                await _ac.sleep(0.5)
                try:
                    if await page.evaluate(_sel_js) == "selected":
                        logger.info(
                            f"dag_challenge: authenticator selected (round {_round + 1}, attempt {_si + 1})"
                        )
                        # Wait for TOTP URL to load
                        for _wi in range(20):
                            await _ac.sleep(0.5)
                            curr_url = page.url
                            if "/challenge/totp" in curr_url or "/challenge/ipp" in curr_url:
                                logger.info("dag_challenge: TOTP page ready")
                                break
                        break
                except Exception:
                    pass
            curr_url = page.url
            if "/challenge/totp" in curr_url or "/challenge/ipp" in curr_url:
                break  # success — exit loop
            logger.info(f"dag_challenge: round {_round + 1} done, still on {curr_url[:60]}")

    # ── TOTP fill (window-aware) ──────────────────────────────────────────
    curr_url = page.url
    _totp_sec = vars_dict.get("totp_secret", "").replace(" ", "").replace("-", "")
    if _pt and _totp_sec and ("/challenge/totp" in curr_url or "/challenge/ipp" in curr_url):
        _rem = 30 - (_tm.time() % 30)
        if _rem < 5:
            logger.info(f"dag_challenge: TOTP window {_rem:.1f}s left — waiting for fresh")
            await _ac.sleep(_rem + 0.8)
        _code = _pt.TOTP(_totp_sec).now()
        logger.info(
            f"dag_challenge: TOTP code=****{_code[-2:]} ({30 - (_tm.time() % 30):.1f}s left)"
        )
        # Poll for visible input
        for _ii in range(14):
            await _ac.sleep(0.5)
            try:
                _vis = await page.evaluate(
                    "(function(){var all=Array.from(document.querySelectorAll('input:not([type=hidden])'));return all.filter(function(i){var r=i.getBoundingClientRect();return r.width>0&&r.height>0;}).length;})()"
                )
                if _vis > 0:
                    break
            except Exception:
                pass
        # Fill via React-compatible native setter
        _fjs = f"""(function(){{var all=Array.from(document.querySelectorAll('input:not([type=hidden])'));var vis=all.filter(function(i){{var r=i.getBoundingClientRect();return r.width>0&&r.height>0;}});if(vis.length>0){{var inp=vis[0];inp.focus();var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(inp,'{_code}');inp.dispatchEvent(new Event('input',{{bubbles:true}}));inp.dispatchEvent(new Event('change',{{bubbles:true}}));return 'filled';}}return 'no_input';}})()"""
        _fr = await page.evaluate(_fjs)
        if _fr != "filled":
            for _fs in ["input[type='tel']", "input[type='number']", "input:not([type='hidden'])"]:
                try:
                    await page.locator(_fs).first.fill(_code, timeout=4000)
                    _fr = "locator"
                    break
                except Exception:
                    pass
        logger.info(f"dag_challenge: TOTP filled ({_fr})")
        await _ac.sleep(0.5)
        # Submit
        _sjs = """(function(){var b=['#totpNext','#next','[jsname="LgbsSe"]','button[type=submit]'];for(var i=0;i<b.length;i++){var el=document.querySelector(b[i]);if(el&&el.getBoundingClientRect().width>0){el.click();return b[i];}}var all=document.querySelectorAll('button,div[role="button"]');for(var i=0;i<all.length;i++){var t=(all[i].innerText||'').toLowerCase();if((t.includes('next')||t.includes('verify'))&&all[i].getBoundingClientRect().width>0){all[i].click();return t;}}return 'not_found';})()"""
        _sb = await page.evaluate(_sjs)
        logger.info(f"dag_challenge: TOTP submit → {_sb}")
        await _ac.sleep(3.0)
        logger.info(f"dag_challenge: post-TOTP URL = {page.url[:80]}")
    else:
        logger.info(f"dag_challenge: skipping inline TOTP ({curr_url[:60]})")


async def _google_post_login_handler(
    page,
    workflow_vars: dict,
    xiosync_base,  # unused — accepted for unified handler signature
    hdrs,  # unused — accepted for unified handler signature
    run_id,  # unused — accepted for unified handler signature
    logger,
    *,
    max_rounds: int = 5,
) -> None:
    """Dismiss post-login interstitials that Google shows after successful authentication.

    Handles (mirrors google-signin.mjs Phase 2.5 dismissal loop):
      - "Stay signed in?" confirmation
      - "Confirm recovery email/phone" prompt
      - "Add a phone number" prompt
      - "Protect your account" security check
      - "Create a passkey" prompt
      - "Turn on 2-Step Verification" prompt
      - "Make sure you can always access your account" prompt

    Also verifies the logged-in email matches the target (P0-4).
    """
    import asyncio as _ac

    _dismiss_selectors = [
        # "Stay signed in?" — Yes/Save
        ("#confirm-button", "stay_signed_in"),
        ('[data-action="confirm"]', "stay_signed_in_alt"),
        ('button[jsname="LgbsSe"]', "google_confirm_btn"),
        # Recovery / security prompts — "Not now" / "Skip" / "Cancel"
        ('button:has-text("Not now")', "not_now"),
        ('button:has-text("Skip")', "skip"),
        ('button:has-text("Cancel")', "cancel"),
        ('button:has-text("No thanks")', "no_thanks"),
        ('button:has-text("Remind me later")', "remind_later"),
        ('a:has-text("Not now")', "not_now_link"),
        ('a:has-text("Skip")', "skip_link"),
        # "Done" after security checkup
        ('button:has-text("Done")', "done"),
    ]

    # Quick text patterns that indicate an interstitial (check in page content)
    _interstitial_signals = [
        "stay signed in",
        "confirm recovery",
        "protect your account",
        "add a phone number",
        "create a passkey",
        "make sure you can always",
        "turn on 2-step",
        "recovery email",
        "home address",
    ]

    for _round in range(max_rounds):
        await _ac.sleep(1.5)
        _url = page.url.lower()

        # If we've left accounts.google.com entirely, we're done
        if "accounts.google.com" not in _url:
            break

        # Check if any interstitial signal is on page
        try:
            _body_text = await page.evaluate("document.body?.innerText?.toLowerCase() || ''")
        except Exception:
            break

        _has_interstitial = any(sig in _body_text for sig in _interstitial_signals)
        if not _has_interstitial:
            # No interstitial detected — we're through
            break

        # Try each dismiss selector
        _dismissed = False
        for _sel, _label in _dismiss_selectors:
            try:
                _loc = page.locator(_sel)
                if await _loc.count() > 0:
                    await _loc.first.click(timeout=3000)
                    logger.info(f"dag_post_login: dismissed '{_label}' (round {_round + 1})")
                    _dismissed = True
                    await _ac.sleep(2.0)
                    break
            except Exception:
                continue

        if not _dismissed:
            logger.info(
                f"dag_post_login: no dismiss button found on round {_round + 1}, URL={page.url[:60]}"
            )
            break

    # ── P0-4: Email verification ─────────────────────────────────────────────────
    _target_email = workflow_vars.get("email", "").lower()
    if _target_email:
        try:
            await page.goto(
                "https://myaccount.google.com/", wait_until="domcontentloaded", timeout=15000
            )
            await _ac.sleep(2.0)
            _final_url = page.url.lower()

            if "about" in _final_url or "signin" in _final_url:
                logger.warning(
                    "dag_post_login: email verify FAILED — redirected to signin, session may be invalid"
                )
            else:
                _body = await page.evaluate("document.body?.innerText?.toLowerCase() || ''")
                if _target_email in _body:
                    logger.info(f"dag_post_login: email verified ✅ ({_target_email})")
                else:
                    logger.warning(
                        f"dag_post_login: email '{_target_email}' NOT found in myaccount body"
                    )
        except Exception as _ve:
            logger.warning(f"dag_post_login: email verify skipped ({_ve})")


# ── Register Google-specific post-action handlers ────────────────────────────
# These fire after the named intent completes successfully in DAGs where
# dag_domain == "accounts.google.com". Other domains register their own.
_DAG_POST_ACTION_HANDLERS[("accounts.google.com", "click_signin")] = [
    _google_challenge_handler,
    _google_post_login_handler,
]
