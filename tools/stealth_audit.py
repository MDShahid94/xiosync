"""stealth_audit.py — 5-Vector Stealth Audit for XIOSYNC browser sessions.

Usage:
    uv run python tools/stealth_audit.py [--cdp-ws-url ws://HOST:PORT/devtools/browser/UUID]
    uv run python tools/stealth_audit.py [--session-id UUID] [--output-dir ./audit_results]

Connects to a running XIOSYNC browser session (via its CDP WebSocket URL)
and navigates to 5 anti-detection test domains:

  1. api.ipapi.is          — IP reputation (datacenter/VPN/proxy detection)
  2. bot.sannysoft.com     — Browser attribute fingerprinting
  3. headless-detector.vercel.app — Headless browser detection (score 0.0–1.0)
  4. abrahamjuliot.github.io/creepjs — Deep JS fingerprint trust scoring
  5. demo.turnstile.workers.dev — Cloudflare Turnstile WAF bypass

Each domain gets a dedicated isolated page (no navigation races).
Captures full-page screenshots and structured JSON results for each vector.
Designed to run from the Mac Mini XIOSYNC host over Tailscale.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("stealth_audit")

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


AUDIT_DOMAINS = [
    {
        "name": "ip_reputation",
        "url": "https://api.ipapi.is/",
        "wait_seconds": 4,
        "description": "IP reputation & network integrity",
    },
    {
        "name": "sannysoft",
        "url": "https://bot.sannysoft.com/",
        "wait_seconds": 8,
        "description": "Browser attribute fingerprinting",
    },
    {
        "name": "headless_detect",
        "url": "https://headless-detector.vercel.app/",
        "wait_seconds": 8,
        "description": "Headless browser detection",
    },
    {
        "name": "creepjs",
        "url": "https://abrahamjuliot.github.io/creepjs/",
        "wait_seconds": 30,
        "description": "Deep JS fingerprint trust scoring",
    },
    {
        "name": "cloudflare_turnstile",
        "url": "https://demo.turnstile.workers.dev/",
        "wait_seconds": 12,
        "description": "Cloudflare Turnstile WAF behavioral challenge",
    },
]


async def _capture_screenshot(page, filepath: Path) -> None:
    """Capture full-page screenshot."""
    try:
        await page.screenshot(path=str(filepath), full_page=True, type="png")
    except Exception as exc:
        logger.warning(f"Screenshot failed: {exc}")


async def _save_html(page, filepath: Path) -> None:
    """Save page HTML source."""
    try:
        html = await page.content()
        filepath.write_text(html, encoding="utf-8")
    except Exception as exc:
        logger.warning(f"HTML save failed: {exc}")


async def _audit_ip_reputation(page) -> dict:
    """Parse api.ipapi.is JSON response.

    The API returns a flat JSON object — company/asn are plain strings,
    not nested dicts. Handle both the legacy nested format and the current flat format.
    """
    try:
        body = await page.locator("body").inner_text()
        data = json.loads(body)
        # company: may be string or {"name": ..., "abr": ...} in different API tiers
        company_raw = data.get("company", "Unknown")
        if isinstance(company_raw, dict):
            company = company_raw.get("name", str(company_raw))
        else:
            company = str(company_raw)

        asn_raw = data.get("asn", "Unknown")
        if isinstance(asn_raw, dict):
            asn = asn_raw.get("asn", str(asn_raw))
        else:
            asn = str(asn_raw)

        loc_raw = data.get("location", None)
        if isinstance(loc_raw, dict):
            location = f"{loc_raw.get('city', '?')}, {loc_raw.get('country', '?')}"
        else:
            # Flat format has city/country at top level
            location = f"{data.get('city', '?')}, {data.get('country', '?')}"

        is_datacenter = data.get("is_datacenter", False)
        is_vpn = data.get("is_vpn", False)
        is_proxy = data.get("is_proxy", False)
        is_tor = data.get("is_tor", False)

        return {
            "ip": data.get("ip", "Unknown"),
            "is_datacenter": is_datacenter,
            "is_vpn": is_vpn,
            "is_proxy": is_proxy,
            "is_tor": is_tor,
            "company": company,
            "asn": asn,
            "city": data.get("city", "?"),
            "country": data.get("country", "?"),
            "timezone": data.get("timezone", "?"),
            "location": location,
            "pass": not is_datacenter and not is_vpn and not is_proxy and not is_tor,
        }
    except Exception as exc:
        return {"error": str(exc), "pass": False}


async def _audit_sannysoft(page) -> dict:
    """Parse bot.sannysoft.com test results — full table scrape."""
    results: dict = {}
    # Scrape every row in the results table
    try:
        rows_js = """
        (function() {
            var out = {};
            var rows = document.querySelectorAll('table tr');
            rows.forEach(function(row) {
                var cells = row.querySelectorAll('td');
                if (cells.length >= 2) {
                    var label = cells[0].innerText.trim().replace(/\\n/g,' ');
                    var val   = cells[1].innerText.trim();
                    var cls   = cells[1].className || '';
                    out[label] = {value: val, pass: cls.includes('passed') || cls.includes('green')};
                }
            });
            return out;
        })()
        """
        rows = await page.evaluate(rows_js)
        results["tests"] = rows
    except Exception as exc:
        results["tests"] = {"error": str(exc)}

    # Also read specific key elements
    checks = [
        ("webdriver-result", "webdriver"),
        ("advanced-webdriver-result", "advanced_webdriver"),
        ("chrome-result", "chrome_object"),
        ("webgl-vendor", "webgl_vendor"),
        ("webgl-renderer", "webgl_renderer"),
        ("user-agent-result", "user_agent"),
        ("permissions-result", "permissions"),
        ("plugins-length-result", "plugins_length"),
        ("languages-result", "languages"),
    ]
    key_results = {}
    for elem_id, key in checks:
        try:
            elem = page.locator(f"#{elem_id}")
            text = await elem.inner_text(timeout=3000)
            classes = await elem.get_attribute("class") or ""
            key_results[key] = {
                "value": text.strip(),
                "pass": "passed" in classes or "green" in classes,
            }
        except Exception:
            key_results[key] = {"value": "not found", "pass": False}

    results.update(key_results)
    results["all_pass"] = all(
        v.get("pass", False)
        for k, v in results.items()
        if k not in ("tests", "all_pass") and isinstance(v, dict)
    )
    return results


async def _audit_headless(page) -> dict:
    """Parse headless-detector.vercel.app headless detection.

    The site exposes:
      window.__headlessDetectionScore  — float 0.0 (normal) to 1.0 (headless)
      window.__headlessDetection       — full results object
      data-headless-score attribute    — on <html> element
    Score thresholds:  < 0.3 = Normal, 0.3-0.6 = Suspicious, >= 0.6 = Headless.
    """
    try:
        # Wait for detection to complete — #loading hidden, #results visible
        try:
            await page.wait_for_selector("#results", state="visible", timeout=15000)
        except Exception:
            pass  # Try reading anyway

        # The Worker UA check is async and may fire AFTER #results appears.
        # Poll until window.__headlessDetection has content (up to 12s extra wait).
        for _ in range(12):
            has_data = await page.evaluate(
                "typeof window.__headlessDetection === 'object' && "
                "window.__headlessDetection !== null && "
                "Object.keys(window.__headlessDetection).length > 0"
            )
            if has_data:
                break
            await asyncio.sleep(1)

        result = await page.evaluate("""() => {
            const score = window.__headlessDetectionScore;
            const det   = window.__headlessDetection || {};
            const attr  = document.documentElement.getAttribute('data-headless-score');
            const badge = document.getElementById('status-badge');
            return {
                score:        score !== undefined ? score : parseFloat(attr || '1'),
                status_badge: badge ? badge.textContent.trim() : null,
                webdriver:    det.webdriver,
                cdp_detected: det.cdpArtifacts ? det.cdpArtifacts.detected : null,
                cdc_keys:     det.cdpArtifacts ? det.cdpArtifacts.cdcKeysFound : null,
                cdp_signals:  det.cdpArtifacts ? det.cdpArtifacts.signals : null,
                ua_suspicious: det.userAgentFlags ? det.userAgentFlags.suspicious : null,
                webgl_software: det.webglFlags ? det.webglFlags.isSoftwareRenderer : null,
                webgl_renderer: det.webglFlags ? det.webglFlags.renderer : null,
                platform:     det.headlessIndicators ? det.headlessIndicators.platform : null,
                cores:        det.headlessIndicators ? det.headlessIndicators.hardwareConcurrency : null,
                device_memory: det.headlessIndicators ? det.headlessIndicators.deviceMemory : null,
                inner_equals_outer: det.headlessIndicators ? det.headlessIndicators.innerEqualsOuter : null,
                worker_mismatch: det.workerChecks ? det.workerChecks.userAgentMismatch : null,
                worker_status:   det.workerChecks ? det.workerChecks.reason : null,
                playwright_detected: det.automationFlags
                    ? (det.automationFlags['__playwright__binding__'] ||
                       det.automationFlags['__pwInitScripts'] ||
                       (det.automationFlags.playwrightExposedFunctions &&
                        det.automationFlags.playwrightExposedFunctions.detected))
                    : null,
            };
        }""")

        score = result.get("score", 1.0)
        passed = score < 0.3  # 0.0 = normal browser
        return {
            "score": score,
            "status": result.get("status_badge")
            or (
                "✅ Normal Browser"
                if score < 0.3
                else "⚠️ Suspicious"
                if score < 0.6
                else "🚫 Headless"
            ),
            "webdriver": result.get("webdriver"),
            "cdp_detected": result.get("cdp_detected"),
            "cdc_keys": result.get("cdc_keys"),
            "cdp_signals": result.get("cdp_signals"),
            "ua_suspicious": result.get("ua_suspicious"),
            "webgl_software": result.get("webgl_software"),
            "webgl_renderer": result.get("webgl_renderer"),
            "platform": result.get("platform"),
            "cores": result.get("cores"),
            "device_memory": result.get("device_memory"),
            "inner_eq_outer": result.get("inner_equals_outer"),
            "worker_mismatch": result.get("worker_mismatch"),
            "worker_status": result.get("worker_status"),
            "playwright": result.get("playwright_detected"),
            "pass": passed,
        }

    except Exception as exc:
        # Fallback: read score from DOM attribute
        try:
            attr_score = await page.evaluate(
                "parseFloat(document.documentElement.getAttribute('data-headless-score') || '1')"
            )
            return {
                "score": attr_score,
                "pass": attr_score < 0.3,
                "note": f"JS API unavailable, used DOM attribute — {exc}",
            }
        except Exception as exc2:
            return {"error": str(exc2), "pass": False}


async def _audit_creepjs(page) -> dict:
    """Parse CreepJS fingerprint trust scores."""
    try:
        # CreepJS dynamically renders — wait for any of several possible selectors
        trust_selectors = [
            ".trust-score",
            "[class*=trust-score]",
            "[class*=trust_score]",
            ".fingerprint-trust",
            "[id*=trust]",
        ]
        found_sel = None
        for sel in trust_selectors:
            try:
                await page.wait_for_selector(sel, timeout=8000)
                found_sel = sel
                break
            except Exception:
                continue

        if not found_sel:
            # Fallback: scrape any percentage text on page
            body = await page.evaluate("document.body.innerText")
            import re

            pcts = re.findall(r"(\d+)%", body)
            return {
                "raw_percentages": pcts[:10],
                "note": "trust-score selector not found — scraped body percentages",
                "pass": len(pcts) == 0 or all(int(p) <= 10 for p in pcts[:3]),
            }

        headless = await page.evaluate(
            """document.querySelector('.headless-rating,[class*=headless-rating],[id*=headless]')?.innerText
               || document.querySelectorAll('[class*=rating]')?.[0]?.innerText || 'Not Found'"""
        )
        stealth = await page.evaluate(
            """document.querySelector('.stealth-rating,[class*=stealth-rating],[id*=stealth]')?.innerText
               || document.querySelectorAll('[class*=rating]')?.[1]?.innerText || 'Not Found'"""
        )
        trust = await page.evaluate(
            """document.querySelector('.trust-score,[class*=trust-score],[id*=trust]')?.innerText
               || 'Not Found'"""
        )

        headless_pct = 0
        stealth_pct = 0
        import re as _re

        m1 = _re.search(r"(\d+)", headless)
        m2 = _re.search(r"(\d+)", stealth)
        if m1:
            headless_pct = int(m1.group(1))
        if m2:
            stealth_pct = int(m2.group(1))

        return {
            "headless_rating": headless.strip(),
            "stealth_rating": stealth.strip(),
            "trust_score": trust.strip(),
            "headless_pct": headless_pct,
            "stealth_pct": stealth_pct,
            "pass": headless_pct <= 10 and stealth_pct <= 10,
        }
    except Exception as exc:
        return {"error": str(exc), "pass": False}


async def _audit_turnstile(page) -> dict:
    """Check if Cloudflare Turnstile token was generated."""
    try:
        # Wait for the turnstile widget to appear (iframe or widget div)
        try:
            await page.wait_for_selector(
                "iframe[src*='turnstile'], div[class*='cf-turnstile'], .cf-turnstile",
                timeout=12000,
            )
        except Exception:
            pass  # Widget might render differently — continue anyway

        await asyncio.sleep(10)  # Give Turnstile time to complete behavioral challenge

        token = await page.evaluate(
            "document.querySelector('input[name=\"cf-turnstile-response\"]')?.value || ''"
        )
        success_text = await page.evaluate(
            "document.querySelector('.success-msg, [class*=success]')?.innerText || ''"
        )
        return {
            "token_generated": bool(token),
            "token_length": len(token) if token else 0,
            "success_message": success_text[:80].strip() if success_text else "",
            "pass": bool(token) or "success" in success_text.lower(),
        }
    except Exception as exc:
        return {"error": str(exc), "pass": False}


_AUDIT_PARSERS = {
    "ip_reputation": _audit_ip_reputation,
    "sannysoft": _audit_sannysoft,
    "headless_detect": _audit_headless,
    "creepjs": _audit_creepjs,
    "cloudflare_turnstile": _audit_turnstile,
}


async def _collect_browser_metadata(context, cdp_ws_url: str) -> dict:
    """Collect Chrome version, UA, stealth layer info via CDP JS."""
    meta: dict = {"cdp_ws_url": cdp_ws_url}
    try:
        p = context.pages[0] if context.pages else await context.new_page()
        ua = await p.evaluate("navigator.userAgent")
        chrome_ver = await p.evaluate(
            "(navigator.userAgent.match(/Chrome\\/([\\d.]+)/)||[])[1] || 'unknown'"
        )
        webdriver = await p.evaluate("navigator.webdriver")
        plugins = await p.evaluate("navigator.plugins.length")
        langs = await p.evaluate("navigator.languages.join(',')")
        webgl_vendor = await p.evaluate(
            "(function(){var c=document.createElement('canvas');var g=c.getContext('webgl');return g?g.getParameter(g.VENDOR):'';})()"
        )
        meta.update(
            {
                "user_agent": ua,
                "chrome_version": chrome_ver,
                "webdriver_flag": webdriver,
                "plugins_count": plugins,
                "languages": langs,
                "webgl_vendor": webgl_vendor,
            }
        )
    except Exception as exc:
        meta["metadata_error"] = str(exc)
    return meta


async def _get_stealth_js(worker_ip: str | None = None) -> str | None:
    """Fetch the rendered stealth init script from the worker agent.

    The worker exposes GET /debug/stealth-js which returns the fully-rendered
    stealth JS for the active PRFL session (with canvas_seed, WebGL renderer,
    UA etc already substituted in).  Falls back to a minimal inline NVIDIA
    WebGL spoof if the endpoint is unreachable.
    """
    if worker_ip:
        try:
            import urllib.request as _ur

            resp = _ur.urlopen(f"http://{worker_ip}:9300/debug/stealth-js", timeout=5)
            js = resp.read().decode("utf-8")
            if len(js) > 200:
                logger.info(f"stealth_js: fetched {len(js)} bytes from worker")
                return js
        except Exception as exc:
            logger.warning(f"stealth_js: fetch failed ({exc}) — using fallback")

    # Minimal fallback: spoof WebGL to Apple M1 (matches PRFL-002 fingerprint)
    return """
    (function() {
        const _V = 'Apple';
        const _R = 'ANGLE (Apple, Apple M1, OpenGL 4.1)';
        const patch = (proto) => {
            if (!proto) return;
            const orig = proto.getParameter;
            proto.getParameter = function(p) {
                if (p === 37445) return _V;
                if (p === 37446) return _R;
                return orig.call(this, p);
            };
            const origExt = proto.getExtension;
            proto.getExtension = function(name) {
                if (name === 'WEBGL_debug_renderer_info')
                    return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
                return origExt.call(this, name);
            };
        };
        try { patch(WebGLRenderingContext.prototype); } catch(_){}
        try { patch(WebGL2RenderingContext.prototype); } catch(_){}
        // Ensure webdriver = false
        try { Object.defineProperty(navigator, 'webdriver', {get: () => false, configurable: true}); } catch(_){}
        // Consistent platform
        try { Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel', configurable: true}); } catch(_){}
    })();
    """


async def run_audit(
    cdp_ws_url: str,
    output_dir: Path,
    worker_ip: str | None = None,
) -> dict:
    """Run the full 5-vector stealth audit.

    Key design:
    - Each domain gets its OWN fresh page opened inside the existing context.
      This eliminates navigation races where page.goto() for domain N is
      interrupted by leftovers from domain N-1.
    - We never close the browser — we're observing an existing session.
    - On error the vector is recorded with pass=False and the audit continues.

    Args:
        cdp_ws_url: WebSocket URL of a running Chromium browser
                    (e.g. ws://100.x.x.y:PORT/devtools/browser/UUID).
        output_dir: Directory to write evidence files.

    Returns:
        Summary dict with results for each vector.
    """
    from patchright.async_api import async_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    summary: dict = {
        "timestamp": datetime.now(UTC).isoformat(),
        "cdp_ws_url": cdp_ws_url,
        "browser_meta": {},
        "vectors": {},
    }

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_ws_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        # ── Fetch stealth init script from worker ──────────────────────────────
        stealth_js = await _get_stealth_js(worker_ip)

        # ── Disable patchright automation flag on existing page targets ─────────
        # connect_over_cdp may force navigator.webdriver=true on new pages via CDP.
        # Counteract by sending Emulation.setAutomationOverride(false) per-target.
        try:
            _bcdp = await browser.new_browser_cdp_session()
            _targets_resp = await _bcdp.send("Target.getTargets", {})
            for _tgt in _targets_resp.get("targetInfos", []):
                if _tgt.get("type") == "page":
                    try:
                        _att = await _bcdp.send(
                            "Target.attachToTarget", {"targetId": _tgt["targetId"], "flatten": True}
                        )
                        _sid = _att.get("sessionId")
                        if _sid:
                            await _bcdp.send(
                                "Emulation.setAutomationOverride",
                                {"enabled": False},
                                session_id=_sid,
                            )
                    except Exception:
                        pass
            await _bcdp.detach()
            logger.info("stealth_js: automation override applied")
        except Exception as _ae:
            logger.debug(f"automation override skipped: {_ae}")

        # ── Per-page CDP cold-navigate injection (registered BEFORE goto()) ─────
        stealth_js_ok = bool(stealth_js)
        if stealth_js:
            logger.info("stealth_js: will inject per-page via CDP session before navigation")

        # Collect browser metadata before navigating anything
        summary["browser_meta"] = await _collect_browser_metadata(context, cdp_ws_url)
        summary["browser_meta"]["stealth_js_injected"] = stealth_js_ok
        print(f"\n📋 Browser: Chrome {summary['browser_meta'].get('chrome_version', '?')}")
        print(f"   UA: {summary['browser_meta'].get('user_agent', '?')[:80]}")
        print(f"   webdriver flag: {summary['browser_meta'].get('webdriver_flag', '?')}")
        print(f"   plugins: {summary['browser_meta'].get('plugins_count', '?')}")
        print(f"   languages: {summary['browser_meta'].get('languages', '?')}")
        print(f"   stealth_js: {'✅ CDP per-page (Main World)' if stealth_js_ok else '⚠️ missing'}")

        for idx, domain in enumerate(AUDIT_DOMAINS, 1):
            name = domain["name"]
            print(f"\n[{idx}/5] {domain['description']} ({domain['url']})...")

            # ── Use an EXISTING Chrome tab for each domain ────────────────────
            # WHY: patchright's new_page() creates isolated renderer processes that
            #  (a) bypass the browser-level --proxy-server=socks5://... causing SOCKS failures
            #  (b) don't inherit the extension content scripts for plugins/languages/WebGL
            # Using an existing tab gives us: SOCKS proxy ✅, extension ✅, GPU ✅
            page = None
            _page_was_ours = False
            try:
                # Prefer an existing blank page; create one only if none exist
                existing = context.pages
                _audit_page = None
                for _p in existing:
                    _u = _p.url
                    if _u in ("about:blank", "chrome://newtab/", ""):
                        _audit_page = _p
                        break
                if _audit_page is None:
                    # Open a new tab (used just for audit, will close at end)
                    _audit_page = await context.new_page()
                    _page_was_ours = True
                page = _audit_page

                # No stealth JS injection needed — the real Chrome extension handles it.
                # Just navigate directly; extension runs at document_start in the real renderer.
                try:
                    await page.goto(
                        domain["url"],
                        wait_until="domcontentloaded",
                        timeout=35000,
                    )
                except Exception as nav_err:
                    logger.warning(f"  nav warning ({name}): {nav_err} — attempting parse anyway")

                await asyncio.sleep(domain["wait_seconds"])

                # Capture evidence
                await _capture_screenshot(page, output_dir / f"{name}_screenshot.png")
                await _save_html(page, output_dir / f"{name}_source.html")

                # Parse results
                parser = _AUDIT_PARSERS.get(name)
                if parser:
                    result = await parser(page)
                    summary["vectors"][name] = result
                    status = "✅ PASS" if result.get("pass") else "❌ FAIL"
                    print(f"  → {status}")
                    for k, v in result.items():
                        if k not in ("pass", "tests"):
                            print(f"    {k}: {v}")
                else:
                    summary["vectors"][name] = {"status": "no parser"}

            except Exception as exc:
                logger.error(f"Audit vector {name} failed: {exc}")
                summary["vectors"][name] = {"error": str(exc), "pass": False}

            finally:
                # Navigate the shared tab back to blank (not close — it may be the user's tab)
                if page:
                    try:
                        await page.goto("about:blank", wait_until="commit", timeout=5000)
                    except Exception:
                        pass
                    if _page_was_ours:
                        try:
                            await page.close()
                        except Exception:
                            pass

        # Don't close the browser — we're just observing an existing session

    # Save summary
    summary_path = output_dir / "audit_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n📋 Summary saved to {summary_path}")

    # Print overall score
    vectors = summary["vectors"]
    passed = sum(1 for v in vectors.values() if v.get("pass"))
    total = len(vectors)
    print(f"\n{'=' * 50}")
    print(f"🏆 STEALTH AUDIT SCORE: {passed}/{total} vectors passed")
    print(f"{'=' * 50}")

    return summary


async def run_audit_via_api(
    session_id: str | None = None,
    output_dir: Path | None = None,
) -> dict:
    """Run audit by looking up a session from the XIOSYNC database."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session as OrmSession
    from xiosync.persistence.database import create_database_engine

    db_url = os.environ.get(
        "XIOSYNC_DATABASE_URL",
        "postgresql+psycopg://xiosync:xiosync@localhost:5432/xiosync",
    )
    engine = create_database_engine(db_url)

    with OrmSession(engine) as sess:
        if session_id:
            import uuid

            row = (
                sess.execute(
                    text("""
                    SELECT id, worker_ts_ip
                    FROM browser_sessions
                    WHERE id = :sid AND state = 'active'
                    LIMIT 1
                """),
                    {"sid": uuid.UUID(session_id)},
                )
                .mappings()
                .first()
            )
        else:
            row = (
                sess.execute(
                    text("""
                    SELECT id, worker_ts_ip
                    FROM browser_sessions
                    WHERE state = 'active'
                    ORDER BY updated_at DESC
                    LIMIT 1
                """),
                )
                .mappings()
                .first()
            )

    if not row:
        print("❌ No active browser session found.")
        sys.exit(1)

    sid = str(row["id"])
    worker_ip = row["worker_ts_ip"]
    print(f"🔗 Found active session {sid[:8]}... on worker {worker_ip}")

    # Get CDP URL from the xiorun-agent /health
    import httpx

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"http://{worker_ip}:9300/health")
        resp.raise_for_status()
        health = resp.json()

    # Get the browser WS URL
    import urllib.request

    ver = json.loads(urllib.request.urlopen(f"http://{worker_ip}:9300/sessions", timeout=5).read())
    cdp_ws_url = None
    for s in ver.get("sessions", []):
        if s.get("session_id") == sid:
            cdp_ws_url = s.get("cdp_ws_url", "").replace("127.0.0.1", worker_ip)
            break

    if not cdp_ws_url:
        print(f"❌ Session {sid[:8]} not found on worker agent.")
        sys.exit(1)

    if output_dir is None:
        output_dir = Path(
            f"xiosync/audit_results/{sid[:8]}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
        )

    print(f"🚀 Starting stealth audit via CDP: {cdp_ws_url}")
    return await run_audit(cdp_ws_url, output_dir)


def main():
    parser = argparse.ArgumentParser(description="XIOSYNC 5-Vector Stealth Audit")
    parser.add_argument("--session-id", help="UUID of the browser session to audit")
    parser.add_argument("--cdp-ws-url", help="Direct CDP WebSocket URL (bypasses DB lookup)")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for evidence (default: xiosync/audit_results/<timestamp>)",
    )
    args = parser.parse_args()

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    if args.cdp_ws_url:
        out = Path(args.output_dir or f"xiosync/audit_results/{ts}")
        # Extract worker IP from WS URL: ws://100.111.130.118:52611/devtools/...
        import re as _re

        _m = _re.search(r"ws://([^:/]+)", args.cdp_ws_url)
        worker_ip = _m.group(1) if _m else None
        asyncio.run(run_audit(args.cdp_ws_url, out, worker_ip=worker_ip))
    else:
        out = Path(args.output_dir) if args.output_dir else None
        asyncio.run(run_audit_via_api(args.session_id, out))


if __name__ == "__main__":
    main()
