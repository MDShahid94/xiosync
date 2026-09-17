"""fingerprint.py — Fingerprint JS injection builder.

Reads a FingerprintProfile ORM row and produces:
  1. JS init script blob   → context.add_init_script()
  2. CDP UA override dict  → Network.setUserAgentOverride

Ported from XIOBR applyFingerprintOverrides() with profile values
sourced from xiogrid_fingerprint_profiles table instead of Tailscale peer data.
"""
from __future__ import annotations

import functools
import json
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xiosync.subsystems.xiogrid.models.exit_node import FingerprintProfile

__all__ = [
    "build_init_script",
    "build_cdp_ua_override",
    "build_uc_options",
    "get_chrome_version",
    "resolve_fingerprint_profile",
]


@functools.lru_cache(maxsize=1)
def get_chrome_version() -> str:
    """Return installed patchright Chromium major version string. Cached."""
    try:
        from patchright.async_api import async_playwright  # noqa: PLC0415
        import asyncio  # noqa: PLC0415

        async def _get() -> str:
            async with async_playwright() as p:
                b = await p.chromium.launch(headless=True, args=["--no-sandbox"])
                ver = b.version
                await b.close()
                return ver.split(".")[0]  # major only, e.g. "131"

        return asyncio.run(_get())
    except Exception:
        return "131"  # safe fallback


def build_init_script(profile: "FingerprintProfile", chrome_ver: str) -> str:
    """Build the JS blob for context.add_init_script().

    Covers 13 fingerprint layers — 8 ported from XIOBR + 5 new:
      1.  navigator.webdriver → undefined
      2.  navigator.platform / hardwareConcurrency / deviceMemory / languages
      3.  navigator.plugins  — 3 fake Chrome built-ins
      4.  window.chrome      — full chrome.app/runtime/etc object
      5.  cdc_* CDP key removal + Object.defineProperty guard
      6.  WebGL vendor/renderer (WebGL1 + WebGL2 + OffscreenCanvas)
      7.  navigator.mediaDevices → cam_name from profile
      8.  RTCPeerConnection ICE server zeroing (no WebRTC IP leak)
      [screen/geometry]  outerWidth/outerHeight/availHeight/dpr fixed
      9.  Canvas fingerprint: per-profile LCG noise on toDataURL + fillText
      10. AudioContext: AnalyserNode float/byte data seeded noise
      11. Timezone: Intl.DateTimeFormat default tz from profile.timezone
      12. Permissions API: notifications → 'denied' (not 'default' as in automation)
      13. navigator.connection: effectiveType/downlink/rtt from profile.connection_type
    """
    ua = profile.ua_template.replace("{cv}", chrome_ver)

    # Build fake plugins array
    fake_plugins = json.dumps([
        {"name": "Chrome PDF Plugin",     "filename": "internal-pdf-viewer",  "description": "Portable Document Format"},
        {"name": "Chrome PDF Viewer",     "filename": "mhjfbmdgcfjbbpaeojofohoefgiehjai", "description": ""},
        {"name": "Native Client",         "filename": "internal-nacl-plugin",  "description": ""},
    ])

    script = f"""
(function () {{
  'use strict';

  // ── 1. navigator.webdriver ────────────────────────────────────────────────
  try {{
    Object.defineProperty(navigator, 'webdriver', {{
      get: () => undefined,
      configurable: true,
    }});
  }} catch (_) {{}}

  // ── 2. navigator platform / hw concurrency / device memory / languages ────
  try {{
    Object.defineProperty(navigator, 'platform',           {{ get: () => {json.dumps(profile.platform)},           configurable: true }});
    Object.defineProperty(navigator, 'hardwareConcurrency',{{ get: () => {profile.cores},                          configurable: true }});
    Object.defineProperty(navigator, 'deviceMemory',       {{ get: () => {profile.ram_gb},                         configurable: true }});
    Object.defineProperty(navigator, 'languages',          {{ get: () => ['en-US', 'en'],                          configurable: true }});
    Object.defineProperty(navigator, 'language',           {{ get: () => 'en-US',                                  configurable: true }});
  }} catch (_) {{}}

  // ── 3. navigator.plugins — 3 fake Chrome built-ins ────────────────────────
  try {{
    const _plugins = {fake_plugins};
    const makePlugin = (p) => Object.create(Plugin.prototype, {{
      name:        {{ value: p.name,        enumerable: true }},
      filename:    {{ value: p.filename,    enumerable: true }},
      description: {{ value: p.description, enumerable: true }},
      length:      {{ value: 0 }},
    }});
    const pluginArr = _plugins.map(makePlugin);
    Object.defineProperty(navigator, 'plugins', {{
      get: () => Object.assign(Object.create(PluginArray.prototype), {{
        ...pluginArr,
        length: pluginArr.length,
        item: (i) => pluginArr[i],
        namedItem: (n) => pluginArr.find(p => p.name === n),
        refresh: () => {{}},
      }}),
      configurable: true,
    }});
    Object.defineProperty(navigator, 'mimeTypes', {{
      get: () => Object.assign(Object.create(MimeTypeArray.prototype), {{ length: 0 }}),
      configurable: true,
    }});
  }} catch (_) {{}}

  // ── 4. window.chrome — full object ────────────────────────────────────────
  try {{
    if (!window.chrome) {{
      window.chrome = {{
        app: {{
          isInstalled: false,
          InstallState: {{ DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }},
          RunningState: {{ CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }},
          getDetails:       () => null,
          getIsInstalled:   () => false,
          installState:     () => 'not_installed',
          runningState:     () => 'cannot_run',
        }},
        runtime: {{
          OnInstalledReason: {{ CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' }},
          OnRestartRequiredReason: {{ APP_UPDATE: 'app_update', GCM_DISABLED: 'gcm_disabled', OS_UPDATE: 'os_update' }},
          PlatformArch: {{ ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' }},
          PlatformNaclArch: {{ ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' }},
          PlatformOs: {{ ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' }},
          RequestUpdateCheckStatus: {{ NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' }},
          id: undefined,
        }},
        csi: () => ({{ onloadT: Date.now(), pageT: Date.now(), startE: Date.now(), tran: 15 }}),
        loadTimes: () => ({{
          commitLoadTime: Date.now() / 1000,
          connectionInfo: 'h2',
          finishDocumentLoadTime: 0,
          finishLoadTime: 0,
          firstPaintAfterLoadTime: 0,
          firstPaintTime: 0,
          navigationType: 'Other',
          npnNegotiatedProtocol: 'h2',
          requestTime: Date.now() / 1000,
          startLoadTime: Date.now() / 1000,
          wasAlternateProtocolAvailable: false,
          wasFetchedViaSpdy: true,
          wasNpnNegotiated: true,
        }}),
      }};
    }}
  }} catch (_) {{}}

  // ── 5. Remove cdc_* CDP leak keys ─────────────────────────────────────────
  try {{
    const _cdcRe = /^cdc_/;
    const _desc  = Object.getOwnPropertyDescriptor;
    for (const key of Object.getOwnPropertyNames(window)) {{
      if (_cdcRe.test(key)) {{
        try {{ delete window[key]; }} catch (_) {{}}
      }}
    }}
    const _origDefine = Object.defineProperty.bind(Object);
    Object.defineProperty = function (obj, prop, desc) {{
      if (typeof prop === 'string' && _cdcRe.test(prop)) return obj;
      return _origDefine(obj, prop, desc);
    }};
  }} catch (_) {{}}

  // ── 6. WebGL vendor / renderer spoof ─────────────────────────────────────
  // Covers WebGL1, WebGL2, OffscreenCanvas WebGL
  (function () {{
    const _vendor   = {json.dumps(profile.webgl_vendor)};
    const _renderer = {json.dumps(profile.webgl_renderer)};
    const _getParam = WebGLRenderingContext.prototype.getParameter;
    const _getParam2 = WebGL2RenderingContext?.prototype?.getParameter;
    function spoofGetParameter(orig) {{
      return function (param) {{
        if (param === 37445) return _vendor;    // UNMASKED_VENDOR_WEBGL
        if (param === 37446) return _renderer;  // UNMASKED_RENDERER_WEBGL
        return orig.call(this, param);
      }};
    }}
    WebGLRenderingContext.prototype.getParameter = spoofGetParameter(_getParam);
    if (_getParam2) WebGL2RenderingContext.prototype.getParameter = spoofGetParameter(_getParam2);
    if (typeof OffscreenCanvas !== 'undefined') {{
      try {{
        const _oc = OffscreenCanvas.prototype.getContext;
        OffscreenCanvas.prototype.getContext = function (type, ...args) {{
          const ctx = _oc.call(this, type, ...args);
          if (ctx && (type === 'webgl' || type === 'webgl2') && ctx.getParameter) {{
            const _orig = ctx.getParameter.bind(ctx);
            ctx.getParameter = function (param) {{
              if (param === 37445) return _vendor;
              if (param === 37446) return _renderer;
              return _orig(param);
            }};
          }}
          return ctx;
        }};
      }} catch (_) {{}}
    }}
  }})();

  // ── 7. mediaDevices.enumerateDevices — fake camera ────────────────────────
  try {{
    const _cam = {json.dumps(profile.cam_name)};
    const _origEnum = navigator.mediaDevices?.enumerateDevices?.bind(navigator.mediaDevices);
    if (_origEnum) {{
      navigator.mediaDevices.enumerateDevices = async function () {{
        const real = await _origEnum();
        if (real.length > 0) return real;
        return [{{
          deviceId: 'default',
          groupId:  'default',
          kind:     'videoinput',
          label:    _cam,
          toJSON:   () => ({{ deviceId: 'default', groupId: 'default', kind: 'videoinput', label: _cam }}),
        }}];
      }};
    }}
  }} catch (_) {{}}

  // ── 8. RTCPeerConnection — zero ICE servers (no WebRTC IP leak) ───────────
  try {{
    const _origRTC = window.RTCPeerConnection;
    if (_origRTC) {{
      window.RTCPeerConnection = function (config, ...args) {{
        const _cfg = config ? {{ ...config, iceServers: [] }} : {{ iceServers: [] }};
        return new _origRTC(_cfg, ...args);
      }};
      Object.assign(window.RTCPeerConnection, _origRTC);
      window.RTCPeerConnection.prototype = _origRTC.prototype;
    }}
  }} catch (_) {{}}

  // ── Screen dimensions + window geometry ──────────────────────────────────
  try {{
    Object.defineProperty(screen, 'width',       {{ get: () => {profile.screen_width},  configurable: true }});
    Object.defineProperty(screen, 'height',      {{ get: () => {profile.screen_height}, configurable: true }});
    Object.defineProperty(screen, 'availWidth',  {{ get: () => {profile.screen_width},  configurable: true }});
    Object.defineProperty(screen, 'availHeight', {{ get: () => {profile.screen_height} - 25, configurable: true }});
    Object.defineProperty(window, 'devicePixelRatio', {{ get: () => {profile.dpr}, configurable: true }});
    // outerHeight/outerWidth = 0 in headless — fix to match viewport so detection fails
    Object.defineProperty(window, 'outerWidth',  {{ get: () => {profile.screen_width},  configurable: true }});
    Object.defineProperty(window, 'outerHeight', {{ get: () => {profile.screen_height}, configurable: true }});
  }} catch (_) {{}}

  // ── 9. Canvas fingerprint noise ───────────────────────────────────────────
  // Injects per-profile deterministic pixel noise into canvas toDataURL/getImageData.
  // The seed is profile-stable so the same "device" always produces the same canvas hash.
  (function () {{
    const _seed = {profile.canvas_seed};
    function _lcg(s) {{ return ((1664525 * s + 1013904223) >>> 0); }}
    let _s = _seed;
    function _noise() {{ _s = _lcg(_s); return ((_s >>> 24) & 0xFF) / 255 * 0.5; }}

    const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function (...args) {{
      const ctx = this.getContext('2d');
      if (ctx) {{
        const imgData = ctx.getImageData(0, 0, this.width, this.height);
        const d = imgData.data;
        // Nudge one random pixel per channel by ±1 to alter canvas hash while keeping visuals clean
        for (let i = 0; i < Math.min(d.length, 16); i += 4) {{
          d[i]     = Math.max(0, Math.min(255, d[i]     + (_noise() < 0.25 ? 1 : 0)));
          d[i + 1] = Math.max(0, Math.min(255, d[i + 1] + (_noise() < 0.25 ? 1 : 0)));
        }}
        ctx.putImageData(imgData, 0, 0);
      }}
      return _origToDataURL.apply(this, args);
    }};

    const _origGetCtx = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type, ...args) {{
      const ctx = _origGetCtx.call(this, type, ...args);
      if (ctx && (type === '2d') && !ctx.__xio_noised) {{
        ctx.__xio_noised = true;
        const _origFill = ctx.fillText.bind(ctx);
        ctx.fillText = function (...a) {{
          _origFill(...a);
          // After any text render, apply micro-noise to prevent font-metric fingerprinting
          const id = ctx.getImageData(0, 0, Math.min(this.canvas.width, 4), 1);
          if (id.data[3] > 0) {{
            id.data[0] = Math.max(0, id.data[0] - (_noise() < 0.3 ? 1 : 0));
            ctx.putImageData(id, 0, 0);
          }}
        }};
      }}
      return ctx;
    }};
  }})();

  // ── 10. AudioContext fingerprint spoof ────────────────────────────────────
  // Hooks OscillatorNode + AnalyserNode to return seeded noise, defeating
  // the standard AudioContext hash fingerprint (used by Cloudflare, FingerprintJS).
  (function () {{
    const _aseed = {profile.audio_seed};
    function _alc(s) {{ return ((22695477 * s + 1) >>> 0); }}
    let _as = _aseed;
    function _anoise() {{ _as = _alc(_as); return ((_as / 4294967296) - 0.5) * 1e-5; }}

    const _AC = window.AudioContext || window.webkitAudioContext;
    if (!_AC) return;
    const _origCreate = _AC.prototype.createAnalyser;
    _AC.prototype.createAnalyser = function (...args) {{
      const node = _origCreate.apply(this, args);
      const _origGetFloat = node.getFloatFrequencyData.bind(node);
      const _origGetByte  = node.getByteFrequencyData.bind(node);
      node.getFloatFrequencyData = function (arr) {{
        _origGetFloat(arr);
        for (let i = 0; i < arr.length; i++) arr[i] += _anoise();
      }};
      node.getByteFrequencyData = function (arr) {{
        _origGetByte(arr);
        for (let i = 0; i < arr.length; i++) arr[i] = Math.max(0, Math.min(255, arr[i] + (_anoise() > 0 ? 1 : 0)));
      }};
      return node;
    }};
  }})();

  // ── 11. Timezone consistency ──────────────────────────────────────────────
  // Must match the PPPoE exit-node geo-IP. Prevents timezone/IP mismatch detection.
  try {{
    const _tz = {json.dumps(getattr(profile, 'timezone', 'America/New_York'))};
    const _origDTF = Intl.DateTimeFormat;
    Intl.DateTimeFormat = function (locale, options) {{
      if (!options || !options.timeZone) {{
        options = {{ ...(options || {{}}), timeZone: _tz }};
      }}
      return new _origDTF(locale, options);
    }};
    Intl.DateTimeFormat.prototype = _origDTF.prototype;
    Object.defineProperty(Intl.DateTimeFormat, 'supportedLocalesOf', {{
      value: _origDTF.supportedLocalesOf.bind(_origDTF), configurable: true,
    }});
  }} catch (_) {{}}

  // ── 12. Permissions API — mask automation defaults ─────────────────────────
  // In automation, Permissions.query({name:'notifications'}) returns 'default'.
  // Real Chrome returns 'denied' or 'prompt'. This inconsistency is a detection signal.
  try {{
    const _origQuery = navigator.permissions?.query?.bind(navigator.permissions);
    if (_origQuery) {{
      navigator.permissions.query = async function (desc) {{
        const result = await _origQuery(desc);
        // Return 'denied' for notifications (real Chrome default when user hasn't chosen)
        if (desc && desc.name === 'notifications' && result.state === 'default') {{
          return Object.create(PermissionStatus.prototype, {{
            state: {{ get: () => 'denied', enumerable: true }},
            onchange: {{ value: null, writable: true }},
          }});
        }}
        return result;
      }};
    }}
  }} catch (_) {{}}

  // ── 13. navigator.connection (NetworkInformation API) ─────────────────────
  try {{
    const _connType = {json.dumps(getattr(profile, 'connection_type', 'wifi'))};
    const _effectiveType = _connType === '4g' ? '4g' : _connType === 'ethernet' ? '4g' : '4g';
    const _downlink = _connType === 'wifi' ? 10 : _connType === 'ethernet' ? 100 : 7.5;
    const _rtt = _connType === 'wifi' ? 50 : _connType === 'ethernet' ? 5 : 100;
    const _fakeConn = {{
      effectiveType: _effectiveType,
      downlink:      _downlink,
      rtt:           _rtt,
      saveData:      false,
      type:          _connType,
      addEventListener: () => {{}},
      removeEventListener: () => {{}},
      dispatchEvent: () => false,
    }};
    Object.defineProperty(navigator, 'connection', {{
      get: () => _fakeConn,
      configurable: true,
    }});
  }} catch (_) {{}}

}})();
"""
    return script


def build_uc_options(profile: "FingerprintProfile", chrome_ver: str, socks5: str = "") -> list[str]:
    """Return Chrome launch arguments for undetected-chromedriver (UC engine).

    These are the proven flags from XIOBR's Colab_Antibot_Browser.ipynb reference
    notebook — the combination that passes Google's bot detection reliably.

    UC binary-patches Chrome itself (renames cdc_* keys at binary level) so these
    flags complement rather than duplicate the JS init_script fingerprint layer.

    Args:
        profile:    FingerprintProfile ORM row for screen/display size.
        chrome_ver: Chrome major version string (e.g. "131").
        socks5:     SOCKS5 proxy address if exit-node is active (e.g. "127.0.0.1:1055").

    Returns:
        List of --flag strings ready to pass to uc.ChromeOptions().add_argument().
    """
    args: list[str] = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--use-gl=angle",
        "--use-angle=swiftshader",
        "--disable-gpu-sandbox",
        "--ignore-gpu-blocklist",
        "--disable-service-workers",
        "--disable-features=ServiceWorker,UserAgentClientHint",
        f"--window-size={profile.screen_width},{profile.screen_height}",
        "--window-position=0,0",
        "--display=:99",
    ]
    if socks5:
        proxy_addr = socks5.replace("socks5://", "").replace("socks4://", "")
        args.append(f"--proxy-server=socks5://{proxy_addr}")
    return args




def build_cdp_ua_override(profile: "FingerprintProfile", chrome_ver: str) -> dict:
    """Build Network.setUserAgentOverride payload for CDP."""
    ua = profile.ua_template.replace("{cv}", chrome_ver)

    brands = [
        {"brand": "Chromium",      "version": chrome_ver},
        {"brand": "Google Chrome", "version": chrome_ver},
        {"brand": "Not=A?Brand",   "version": "99"},
    ]

    return {
        "userAgent": ua,
        "platform":  profile.platform,
        "userAgentMetadata": {
            "brands":           brands,
            "platform":         profile.ch_platform,
            "architecture":     profile.ch_arch,
            "mobile":           profile.is_mobile,
            "bitness":          "64",
            "wow64":            False,
            "fullVersion":      f"{chrome_ver}.0.0.0",
            "fullVersionList":  brands,
        },
    }


def resolve_fingerprint_profile(
    session_id: str,
    engine: object,
) -> "FingerprintProfile | None":
    """Resolve FingerprintProfile for a session via PPPoE exit node chain.

    Lookup path:
        browser_sessions.pppoe_exit_node_id
        → PPPoEExitNode.fingerprint_profile_id
        → FingerprintProfile

    Falls back to first os='macos' profile if exit node not set.
    Returns None only if no profiles seeded at all (should never happen post-bootstrap).
    """
    from sqlalchemy import select, text  # noqa: PLC0415
    from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415
    from xiosync.subsystems.xiogrid.models.browser import BrowserSession  # noqa: PLC0415
    from xiosync.subsystems.xiogrid.models.exit_node import (  # noqa: PLC0415
        FingerprintProfile,
        PPPoEExitNode,
    )
    import uuid  # noqa: PLC0415

    with OrmSession(engine) as sess:
        # Step 1: resolve via exit node chain
        row = sess.execute(
            text("""
                SELECT fp.*
                FROM browser_sessions bs
                JOIN xiogrid_pppoe_exit_nodes en ON en.id = bs.pppoe_exit_node_id
                JOIN xiogrid_fingerprint_profiles fp ON fp.id = en.fingerprint_profile_id
                WHERE bs.id = :sid
                LIMIT 1
            """),
            {"sid": uuid.UUID(session_id)},
        ).mappings().first()

        if row:
            # Re-fetch as ORM object for type safety
            return sess.get(FingerprintProfile, row["id"])

        # Step 2: fallback — first macos profile
        fp = sess.scalars(
            select(FingerprintProfile)
            .where(FingerprintProfile.os == "macos")
            .limit(1)
        ).first()
        if fp:
            return fp

        # Step 3: any profile at all
        return sess.scalars(select(FingerprintProfile).limit(1)).first()
