"""Stealth JavaScript injection and browser hardening configurations.

Spoofs navigator properties, WebGL, Canvas, AudioContext, Screen,
Permissions, Geolocation, Timezone, and removes CDP automation artifacts.
"""

from __future__ import annotations

import datetime
from typing import Any
import zoneinfo

# UA must EXACTLY match the installed Chrome binary version.
# Chrome 131.0.6778.108 = last version UC 3.5.5 fully patches (navigator.webdriver=False confirmed).
# Chrome 153 is NOT used — UC 3.5.5 doesn't patch it and Google gives only 3 cookies.
_CHROME_VERSION = "131.0.6778.108"  # /opt/chrome131/chrome
_STEALTH_UA_CHROME = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{_CHROME_VERSION} Safari/537.36"
)

# Chromium launch args (hardened, stealth, Colab-compatible)
CHROMIUM_ARGS = [
    "--no-sandbox",
    # NOTE: --disable-setuid-sandbox removed — shows a visible warning banner
    # ("unsupported command-line flag") which is a bot detection signal.
    # Running as root in Colab, --no-sandbox alone is sufficient.
    "--disable-dev-shm-usage",
    "--ignore-gpu-blocklist",
    # Anti-automation detection
    "--disable-blink-features=AutomationControlled",
    "--disable-automation",
    "--disable-infobars",
    "--remote-debugging-address=0.0.0.0",  # bind to Tailscale IP
    # Language
    "--lang=en-US,en",
    "--accept-lang=en-US,en;q=0.9,en-GB;q=0.8",
    # WebGL: use ANGLE over EGL/Mesa (not SwiftShader Vulkan).
    # SwiftShader-Vulkan is a known headless indicator — EGL-ANGLE is far more
    # realistic and still works on Xvfb with Mesa drivers.
    "--use-gl=angle",
    "--use-angle=gl",  # gl = EGL/Mesa (more realistic than swiftshader)
    "--enable-webgl",
    "--enable-webgl2",
    # Window geometry (800x600 default = instant bot flag)
    "--window-size=1920,1080",
    "--start-maximized",
    # Session stability
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    # Fix: Trusted Types blocks blob Worker() in headless-detector V4 check.
    # Disabling TrustedDOMTypes allows importScripts()-based Worker UA patching.
    "--disable-features=TrustedDOMTypes",
    # NOTE: do NOT add --disable-extensions-except= — it blocks stealth extension
]

# ── Stealth init script — comprehensive fingerprint spoofing ──────────────────
# Template placeholders replaced at launch time by Python:
#   {TIMEZONE}     IANA tz string  e.g. "Asia/Kolkata"
#   {LOCALE}       BCP-47 locale   e.g. "en-IN"
#   {CANVAS_SEED}  int32 seed for canvas noise
#   {AUDIO_SEED}   int32 seed for audio noise
#   {LAT}          float latitude  for geolocation
#   {LON}          float longitude for geolocation
#   {UA}           full User-Agent string
#   {TZ_OFFSET}    int minutes west of UTC (e.g. -330 for IST)
_STEALTH_JS = r"""
(function () {
  'use strict';


  // ── Jitter timing APIs to prevent extension measurement ───────────────────
  const _origPerfNow = performance.now;
  performance.now = function() { return _origPerfNow.call(this) + (Math.random() * 0.05); };
  const _origDateNow = Date.now;
  Date.now = function() { return _origDateNow.call(this) + Math.floor(Math.random() * 5); };
  // ── 1. navigator.webdriver → delete from prototype (avoids webdriver_getter_modified signal) ──
  // Object.defineProperty on the *instance* leaves a detectable custom descriptor.
  // Deleting from Navigator.prototype means getOwnPropertyDescriptor(navigator,'webdriver')
  // returns undefined — indistinguishable from a browser that never had the property.
  try {
    delete Navigator.prototype.webdriver;
  } catch (_) {
    // Fallback: redefine on prototype (not instance) — descriptor still looks native
    try {
      Object.defineProperty(Navigator.prototype, 'webdriver', {
        get: () => undefined, configurable: true, enumerable: false,
      });
    } catch (_2) {}
  }

  // ── 2. Remove CDP residual cdc_ keys ─────────────────────────────────────
  Object.getOwnPropertyNames(window).filter(k => k.match(/^cdc_/))
    .forEach(k => { try { delete window[k]; } catch (_) {} });

  // ── 2b. Worker UA spoof — intercept Worker() so Web Workers report the ────
  // same spoofed UA as the main thread. Skips module workers (importScripts()
  // is forbidden there) to avoid breaking Cloudflare Turnstile etc.
  try {
    const _SPOOF_UA = '{UA}';
    const _CORES    = {CORES};
    const _RAM      = {RAM};
    const _OW = window.Worker;
    window.Worker = function(url, opts) {
      // Never touch module workers — importScripts() throws in module context.
      if (opts && opts.type === 'module') return new _OW(url, opts);
      if (typeof url !== 'string') return new _OW(url, opts);
      try {
        const _patch = [
          'Object.defineProperty(self.navigator,"userAgent",{get:()=>',
          JSON.stringify(_SPOOF_UA), ',configurable:true,enumerable:true});',
          'Object.defineProperty(self.navigator,"platform",{get:()=>"Win32",configurable:true});',
          'Object.defineProperty(self.navigator,"appVersion",{get:()=>',
          JSON.stringify(_SPOOF_UA.replace('Mozilla/','')), ',configurable:true});',
          'Object.defineProperty(self.navigator,"vendor",{get:()=>"Google Inc.",configurable:true});',
          'Object.defineProperty(self.navigator,"hardwareConcurrency",{get:()=>',String(_CORES),',configurable:true});',
          'Object.defineProperty(self.navigator,"deviceMemory",{get:()=>',String(_RAM),',configurable:true});',
        ].join('');
        const _pBlob = new Blob([_patch], {type:'text/javascript'});
        const _pUrl  = URL.createObjectURL(_pBlob);
        const _chain = new Blob(
          ['importScripts('+JSON.stringify(_pUrl)+','+JSON.stringify(url)+');'],
          {type:'text/javascript'}
        );
        const _wUrl = URL.createObjectURL(_chain);
        const _w = new _OW(_wUrl, opts);
        return _w;
      } catch(_) { return new _OW(url, opts); }
    };
    window.Worker.prototype = _OW.prototype;
    Object.defineProperty(window.Worker,'toString',
      {value:()=>'function Worker() { [native code] }',configurable:true});
  } catch (_) {}


  // ── 3. navigator.plugins / mimeTypes / language / languages ─────────────
  // Chrome 131 made navigator.plugins non-configurable — Object.defineProperty
  // silently fails on both instance and prototype levels.
  // Solution: Replace window.navigator with a Proxy that intercepts specific props.
  // window.navigator IS configurable (not LegacyUnforgeable on Window).
  const _fakePlugins = [
    { name: 'Chrome PDF Plugin',          filename: 'internal-pdf-viewer',          description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer',          filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
    { name: 'Native Client',              filename: 'internal-nacl-plugin',          description: '' },
    { name: 'WebKit built-in PDF',        filename: 'internal-pdf-viewer',           description: '' },
    { name: 'Widevine Content Decryption Module', filename: 'widevinecdmadapter.dll', description: 'Enables Widevine licenses for play back of HTML audio/video content.' },
  ];
  const _buildFakePluginArr = () => {
    const arr = [];
    _fakePlugins.forEach((p, i) => {
      const plug = (typeof Plugin !== 'undefined')
        ? Object.assign(Object.create(Plugin.prototype), p)
        : { ...p };
      arr[i] = plug;
    });
    try { Object.defineProperty(arr, 'item',      { value: i => arr[i], configurable: true }); } catch(_){}
    try { Object.defineProperty(arr, 'namedItem', { value: n => arr.find(p => p.name === n), configurable: true }); } catch(_){}
    try { Object.defineProperty(arr, 'length',    { value: 5, writable: false, configurable: false, enumerable: false }); } catch(_){}
    if (typeof PluginArray !== 'undefined') { try { Object.setPrototypeOf(arr, PluginArray.prototype); } catch(_){} }
    return arr;
  };
  const _fakeMimeTypes = (() => {
    const mt = [
      { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
      { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' },
    ];
    try { Object.defineProperty(mt, 'length', { value: 2, writable: false }); } catch(_){}
    try { Object.defineProperty(mt, 'item',   { value: i => mt[i] }); } catch(_){}
    try { Object.defineProperty(mt, 'namedItem', { value: n => mt.find(m => m.type === n) }); } catch(_){}
    if (typeof MimeTypeArray !== 'undefined') { try { Object.setPrototypeOf(mt, MimeTypeArray.prototype); } catch(_){} }
    return mt;
  })();

  // ── Navigator Proxy — the nuclear option for non-configurable properties ──
  // Chrome 131 locked plugins/languages via [LegacyUnforgeable]-like IDL attrs.
  // We swap out window.navigator itself with a Proxy. This works because
  // window.navigator (on Window.prototype) IS configurable.
  try {
    const _realNav = window.navigator;
    const _navProxy = new Proxy(_realNav, {
      get(target, prop) {
        // ── Navigator property spoofs (all in one place) ──
        if (prop === 'plugins')              return _buildFakePluginArr();
        if (prop === 'mimeTypes')            return _fakeMimeTypes;
        if (prop === 'languages')            return [_LOCALE, _LANG];
        if (prop === 'language')             return _LOCALE;
        if (prop === 'userAgent')            return _UA;
        if (prop === 'appVersion')           return _UA.replace('Mozilla/', '');
        if (prop === 'platform')             return '{PLATFORM}';
        if (prop === 'hardwareConcurrency')  return {CORES};
        if (prop === 'deviceMemory')         return {RAM};
        if (prop === 'vendor')               return 'Google Inc.';
        if (prop === 'vendorSub')            return '';
        if (prop === 'productSub')           return '20030107';
        if (prop === 'maxTouchPoints')       return 0;
        if (prop === 'webdriver')            return false;
        if (prop === 'doNotTrack')           return null;
        // All other props: reflect from real navigator (bound to avoid 'this' issues)
        const val = Reflect.get(target, prop, target);
        return (typeof val === 'function') ? val.bind(target) : val;
      },
      has(target, prop) { return prop in target; },
      getOwnPropertyDescriptor(target, prop) {
        // Make proxy transparent to detection attempts
        return Object.getOwnPropertyDescriptor(target, prop);
      },
    });
    // Replace window.navigator with the proxy
    Object.defineProperty(window, 'navigator', {
      get: () => _navProxy,
      configurable: true, enumerable: true,
    });
  } catch (_) {
    // Fallback: prototype + instance patches if Proxy fails
    try { Object.defineProperty(Navigator.prototype, 'plugins',   { get: () => _buildFakePluginArr(), configurable: true }); } catch(_){}
    try { Object.defineProperty(Navigator.prototype, 'mimeTypes', { get: () => _fakeMimeTypes, configurable: true }); } catch(_){}
    try { Object.defineProperty(Navigator.prototype, 'languages', { get: () => [_LOCALE, _LANG], configurable: true }); } catch(_){}
    try { Object.defineProperty(navigator, 'plugins',   { get: () => _buildFakePluginArr(), configurable: true }); } catch(_){}
    try { Object.defineProperty(navigator, 'mimeTypes', { get: () => _fakeMimeTypes, configurable: true }); } catch(_){}
    try { Object.defineProperty(navigator, 'languages', { get: () => [_LOCALE, _LANG], configurable: true }); } catch(_){}
  }



  // ── 4. window.chrome — full runtime object Google checks ─────────────────
  if (!window.chrome) {
    try {
      Object.defineProperty(window, 'chrome', {
        value: {
          app: {
            isInstalled: false,
            InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
            RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
          },
          runtime: {
            OnInstalledReason: {}, OnRestartRequiredReason: {}, PlatformArch: {},
            PlatformNaclArch: {}, PlatformOs: {}, RequestUpdateCheckStatus: {}, id: undefined,
            connect: () => {}, sendMessage: () => {},
          },
          loadTimes: function() { return { firstPaintTime: performance.now()/1000 - 0.05 }; },
          csi: function() { return { startE: Date.now() - 1000, onloadT: Date.now(), pageT: 1000, tran: 15 }; },
        },
        configurable: true, writable: false,
      });
    } catch (_) {}
  }

  // ── 5. Language / Locale (IP-geo injected) ────────────────────────────────
  const _LOCALE = '{LOCALE}';
  const _LANG   = _LOCALE.split('-')[0];
  // Prototype-level patch → works even in restricted CDP/patchright contexts
  try { Object.defineProperty(Navigator.prototype, 'language',  { get: function() { return _LOCALE; }, configurable: true, enumerable: true }); } catch (_) {}
  try { Object.defineProperty(Navigator.prototype, 'languages', { get: function() { return [_LOCALE, _LANG]; }, configurable: true, enumerable: true }); } catch (_) {}
  // Instance-level backup (belt-and-suspenders)
  try { Object.defineProperty(navigator, 'language',  { get: () => _LOCALE, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'languages', { get: () => [_LOCALE, _LANG], configurable: true }); } catch (_) {}

  // ── 6. User-Agent + related navigator props ───────────────────────────────
  const _UA = '{UA}';
  // Prototype-level patches (primary — survive patchright context restrictions)
  const _def = (proto, key, val) => {
    try { Object.defineProperty(proto, key, { get: typeof val === 'function' ? val : () => val, configurable: true, enumerable: true }); } catch (_) {}
  };
  _def(Navigator.prototype, 'userAgent',          () => _UA);
  _def(Navigator.prototype, 'appVersion',         () => _UA.replace('Mozilla/', ''));
  _def(Navigator.prototype, 'hardwareConcurrency',() => {CORES});
  _def(Navigator.prototype, 'deviceMemory',       () => {RAM});
  _def(Navigator.prototype, 'platform',           () => '{PLATFORM}');
  _def(Navigator.prototype, 'vendor',             () => 'Google Inc.');
  _def(Navigator.prototype, 'vendorSub',          () => '');
  _def(Navigator.prototype, 'productSub',         () => '20030107');
  _def(Navigator.prototype, 'maxTouchPoints',     () => 0);
  _def(Navigator.prototype, 'cookieEnabled',      () => true);
  _def(Navigator.prototype, 'onLine',             () => true);
  _def(Navigator.prototype, 'doNotTrack',         () => null);
  // Instance-level fallback (some properties need own-property to shadow prototype)
  try { Object.defineProperty(navigator, 'userAgent',           { get: () => _UA, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'appVersion',          { get: () => _UA.replace('Mozilla/', ''), configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => {CORES}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'deviceMemory',        { get: () => {RAM}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'platform',            { get: () => '{PLATFORM}', configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'vendor',              { get: () => 'Google Inc.', configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'vendorSub',           { get: () => '', configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'productSub',          { get: () => '20030107', configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'maxTouchPoints',      { get: () => 0, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'cookieEnabled',       { get: () => true, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'onLine',              { get: () => true, configurable: true }); } catch (_) {}
  try { Object.defineProperty(navigator, 'doNotTrack',          { get: () => null, configurable: true }); } catch (_) {}


  // ── 6b. UserAgentData (Client Hints) spoofing ───────────────────────────
  try {
    const _chPlatform = '{CH_PLATFORM}';
    const _mobile = {IS_MOBILE};
    const _brands = [
      {brand: 'Chromium', version: '131'},
      {brand: 'Google Chrome', version: '131'},
      {brand: 'Not_A Brand', version: '24'}
    ];
    Object.defineProperty(navigator, 'userAgentData', {
      get: () => ({
        brands: _brands,
        mobile: _mobile,
        platform: _chPlatform,
        getHighEntropyValues: (hints) => Promise.resolve({
          brands: _brands,
          mobile: _mobile,
          platform: _chPlatform,
          platformVersion: '10.0.0',
          architecture: '{CH_ARCH}',
          model: '',
          bitness: '64',
          fullVersionList: _brands
        })
      }),
      configurable: true
    });
  } catch (_) {}

  // ── 7. Screen dimensions ──────────────────────────────────────────────────
  try { Object.defineProperty(screen, 'width',       { get: () => {SCREEN_W}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'height',      { get: () => {SCREEN_H}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'availWidth',  { get: () => {SCREEN_W}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'availHeight', { get: () => {SCREEN_AH}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'availLeft',   { get: () => 0, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'availTop',    { get: () => 0, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'colorDepth',  { get: () => 24, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'pixelDepth',  { get: () => 24, configurable: true }); } catch (_) {}
  try { Object.defineProperty(screen, 'orientation', { get: () => ({ type: 'landscape-primary', angle: 0 }), configurable: true }); } catch (_) {}
  try { Object.defineProperty(window, 'devicePixelRatio', { get: () => 1, configurable: true }); } catch (_) {}
  try { Object.defineProperty(window, 'outerWidth',  { get: () => {SCREEN_W}, configurable: true }); } catch (_) {}
  try { Object.defineProperty(window, 'outerHeight', { get: () => {SCREEN_H}, configurable: true }); } catch (_) {}

  // ── 8. WebGL vendor / renderer ────────────────────────────────────────────
  // Chrome 131: WebGLRenderingContext.prototype.getParameter is NON-WRITABLE.
  // Direct assignment (proto.getParameter = ...) silently fails in sloppy mode.
  // Solution: Intercept HTMLCanvasElement.prototype.getContext — it IS configurable.
  // Patch each context INSTANCE via Object.defineProperty (own props > prototype).
  const _WEBGL_VENDOR   = '{WEBGL_V}';
  const _WEBGL_RENDERER = '{WEBGL_R}';

  const _patchCtxInstance = (ctx) => {
    if (!ctx) return ctx;
    // Own-property override — takes priority over non-writable prototype method
    const _origGP  = ctx.getParameter.bind(ctx);
    const _origGE  = ctx.getExtension.bind(ctx);
    try {
      Object.defineProperty(ctx, 'getParameter', {
        value: function(p) {
          if (p === 37445) return _WEBGL_VENDOR;
          if (p === 37446) return _WEBGL_RENDERER;
          return _origGP(p);
        }, writable: true, configurable: true, enumerable: false,
      });
    } catch(_){}
    try {
      Object.defineProperty(ctx, 'getExtension', {
        value: function(name) {
          if (name === 'WEBGL_debug_renderer_info') {
            return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
          }
          return _origGE(name);
        }, writable: true, configurable: true, enumerable: false,
      });
    } catch(_){}
    return ctx;
  };

  // Intercept canvas.getContext at HTMLCanvasElement prototype level (IS configurable)
  try {
    const _origGetCtx = HTMLCanvasElement.prototype.getContext;
    Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
      value: function(type, ...args) {
        const ctx = _origGetCtx.apply(this, [type, ...args]);
        if (ctx && (type === 'webgl' || type === 'webgl2' || type === 'experimental-webgl')) {
          return _patchCtxInstance(ctx);
        }
        return ctx;
      }, writable: true, configurable: true, enumerable: false,
    });
  } catch(_){}

  // Belt-and-suspenders: also try prototype-level (may work in some contexts)
  const _patchWebGLProto = (proto) => {
    if (!proto) return;
    try {
      const _o = proto.getParameter;
      Object.defineProperty(proto, 'getParameter', {
        value: function(p) {
          if (p === 37445) return _WEBGL_VENDOR;
          if (p === 37446) return _WEBGL_RENDERER;
          return _o.call(this, p);
        }, writable: true, configurable: true, enumerable: false
      });
    } catch(_){}
    try {
      const _e = proto.getExtension;
      Object.defineProperty(proto, 'getExtension', {
        value: function(name) {
          if (name === 'WEBGL_debug_renderer_info')
            return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
          return _e.call(this, name);
        }, writable: true, configurable: true, enumerable: false
      });
    } catch(_){}
  };
  try { _patchWebGLProto(WebGLRenderingContext.prototype); } catch(_){}
  try { _patchWebGLProto(WebGL2RenderingContext.prototype); } catch(_){}


  // ── 9. Timezone — Intl + Date.prototype ──────────────────────────────────
  const _XIO_TZ     = '{TIMEZONE}';
  const _XIO_OFFSET = {TZ_OFFSET};   // minutes west of UTC (e.g. -330 for IST = UTC+5:30)
  if (_XIO_TZ && _XIO_TZ !== 'UTC') {
    try {
      const _OrigDTF = Intl.DateTimeFormat;
      const _PatchedDTF = function(locale, opts) {
        opts = Object.assign({}, opts || {});
        if (!opts.timeZone) opts.timeZone = _XIO_TZ;
        return new _OrigDTF(locale || _LOCALE, opts);
      };
      Object.setPrototypeOf(_PatchedDTF, _OrigDTF);
      _PatchedDTF.prototype = _OrigDTF.prototype;
      _PatchedDTF.supportedLocalesOf = _OrigDTF.supportedLocalesOf.bind(_OrigDTF);
      Intl.DateTimeFormat = _PatchedDTF;
    } catch (_) {}
    // Also patch Date.prototype.getTimezoneOffset
    try {
      const _origGTO = Date.prototype.getTimezoneOffset;
      Date.prototype.getTimezoneOffset = function() { return _XIO_OFFSET; };
    } catch (_) {}
    // Intl.DateTimeFormat().resolvedOptions().timeZone
    try {
      const _origRO = Intl.DateTimeFormat.prototype.resolvedOptions;
      Intl.DateTimeFormat.prototype.resolvedOptions = function() {
        const r = _origRO.call(this);
        r.timeZone = _XIO_TZ;
        r.locale   = _LOCALE;
        return r;
      };
    } catch (_) {}
  }

  // ── 10. WebRTC — force TURN-relay (blocks local/STUN IP leak) ────────────
  try {
    const _OrigRTC = window.RTCPeerConnection;
    if (_OrigRTC) {
      const _PatchRTC = function(cfg, ...rest) {
        if (cfg) cfg.iceTransportPolicy = 'relay';
        return new _OrigRTC(cfg, ...rest);
      };
      _PatchRTC.prototype = _OrigRTC.prototype;
      window.RTCPeerConnection = _PatchRTC;
      window.webkitRTCPeerConnection = _PatchRTC;
    }
  } catch (_) {}

  // ── 11. Network connection — spoof realistic broadband ───────────────────
  try {
    Object.defineProperty(navigator, 'connection', {
      get: () => ({
        effectiveType: '4g', rtt: 50, downlink: 10.0,
        type: 'wifi', saveData: false,
        addEventListener: () => {}, removeEventListener: () => {},
        dispatchEvent: () => true,
      }),
      configurable: true,
    });
  } catch (_) {}

  // ── 12. Canvas fingerprint noise (seeded, deterministic) ─────────────────
  const _CSEED = {CANVAS_SEED};
  if (_CSEED) {
    try {
      const _origTDU = HTMLCanvasElement.prototype.toDataURL;
      HTMLCanvasElement.prototype.toDataURL = function(...a) {
        const d = _origTDU.apply(this, a);
        const i = d.lastIndexOf(',');
        if (i < 0) return d;
        const noise = (_CSEED ^ 0xDEADBEEF).toString(16).padStart(8, '0');
        return d.slice(0, i + 1) + noise + d.slice(i + 9);
      };
      const _origGID = CanvasRenderingContext2D.prototype.getImageData;
      CanvasRenderingContext2D.prototype.getImageData = function(...a) {
        const imgData = _origGID.apply(this, a);
        const d = imgData.data;
        const n = (_CSEED & 0xFF);
        for (let i = 0; i < d.length; i += 400) { d[i] = (d[i] + n) & 0xFF; }
        return imgData;
      };
    } catch (_) {}
  }

  // ── 13. AudioContext fingerprint noise (seeded, deterministic) ───────────
  const _ASEED = {AUDIO_SEED};
  if (_ASEED) {
    try {
      const _origGCD = AudioBuffer.prototype.getChannelData;
      AudioBuffer.prototype.getChannelData = function(ch) {
        const d = _origGCD.call(this, ch);
        for (let i = 0; i < d.length; i += 100) { d[i] = d[i] + (_ASEED * 1e-7); }
        return d;
      };
      // Also patch AudioContext.createOscillator to add subtle frequency shift
      const _origCO = (window.AudioContext || window.webkitAudioContext || class {}).prototype;
      if (_origCO && _origCO.createOscillator) {
        const _origOsc = _origCO.createOscillator;
        _origCO.createOscillator = function() {
          const osc = _origOsc.call(this);
          const origFreq = Object.getOwnPropertyDescriptor(OscillatorNode.prototype, 'frequency');
          return osc;
        };
      }
    } catch (_) {}
  }

  // ── 14. Battery API — spoof realistic laptop battery ─────────────────────
  try {
    const _fakeBattery = {
      charging: true, chargingTime: 0, dischargingTime: Infinity, level: 0.87,
      addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => true,
    };
    if (navigator.getBattery) {
      navigator.getBattery = () => Promise.resolve(_fakeBattery);
    }
  } catch (_) {}

  // ── 15. Speech synthesis — spoof 2 realistic voices ─────────────────────
  try {
    const _fakeVoices = [
      { voiceURI: 'Google US English', name: 'Google US English', lang: _LOCALE,
        localService: false, default: true },
      { voiceURI: 'Google UK English Female', name: 'Google UK English Female',
        lang: 'en-GB', localService: false, default: false },
    ];
    if (window.speechSynthesis) {
      window.speechSynthesis.getVoices = () => _fakeVoices;
    }
  } catch (_) {}

  // ── 16. Permissions API — grant realistic set ────────────────────────────
  try {
    if (navigator.permissions && navigator.permissions.query) {
      const _origQuery = navigator.permissions.query.bind(navigator.permissions);
      navigator.permissions.query = (desc) => {
        const name = (desc || {}).name;
        if (name === 'notifications') return Promise.resolve({ state: 'denied',  onchange: null });
        if (name === 'geolocation')   return Promise.resolve({ state: 'granted', onchange: null });
        if (name === 'camera')        return Promise.resolve({ state: 'prompt',  onchange: null });
        if (name === 'microphone')    return Promise.resolve({ state: 'prompt',  onchange: null });
        if (name === 'clipboard-read')return Promise.resolve({ state: 'prompt',  onchange: null });
        return _origQuery(desc).catch(() => Promise.resolve({ state: 'prompt', onchange: null }));
      };
    }
  } catch (_) {}

  // ── 17. MediaDevices — spoof one camera + one mic ────────────────────────
  try {
    if (navigator.mediaDevices) {
      navigator.mediaDevices.enumerateDevices = () => Promise.resolve([
        { deviceId: 'default', kind: 'audioinput',  label: '', groupId: 'default' },
        { deviceId: 'default', kind: 'videoinput',  label: '', groupId: 'default' },
        { deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'default' },
      ]);
    }
  } catch (_) {}

  // ── 18. Geolocation — spoof via IP-geo lat/lon ───────────────────────────
  const _GEO_LAT = {LAT};
  const _GEO_LON = {LON};
  try {
    navigator.geolocation.getCurrentPosition = (success) => {
      success({
        coords: {
          latitude: _GEO_LAT, longitude: _GEO_LON, accuracy: 1000,
          altitude: null, altitudeAccuracy: null, heading: null, speed: null,
        },
        timestamp: Date.now(),
      });
    };
    navigator.geolocation.watchPosition = (success) => {
      success({
        coords: {
          latitude: _GEO_LAT, longitude: _GEO_LON, accuracy: 1000,
          altitude: null, altitudeAccuracy: null, heading: null, speed: null,
        },
        timestamp: Date.now(),
      });
      return 1;
    };
  } catch (_) {}

  // ── 19. ClientRects noise — subtle per-session layout fingerprint noise ──
  const _CRECT_SEED = {CANVAS_SEED} ^ 0xBEEF;
  try {
    const _origGBCR = Element.prototype.getBoundingClientRect;
    Element.prototype.getBoundingClientRect = function() {
      const r = _origGBCR.call(this);
      const noise = (_CRECT_SEED % 3) * 0.0001;
      return {
        top: r.top + noise, left: r.left + noise,
        right: r.right + noise, bottom: r.bottom + noise,
        width: r.width, height: r.height, x: r.x + noise, y: r.y + noise,
        toJSON: r.toJSON ? r.toJSON.bind(r) : undefined,
      };
    };
  } catch (_) {}

  // ── 20. History length — spoof realistic non-zero ────────────────────────
  try {
    Object.defineProperty(history, 'length', { get: () => 3, configurable: true });
  } catch (_) {}

  // ── 21. Font enumeration block (canvas-based font detect) ────────────────
  // Patches measureText so font detection via canvas probing returns consistent values
  try {
    const _origMT = CanvasRenderingContext2D.prototype.measureText;
    const _FONT_SEED = ({CANVAS_SEED} & 0xFFFF);
    CanvasRenderingContext2D.prototype.measureText = function(text) {
      const m = _origMT.call(this, text);
      // Perturb width slightly to confuse font detection while staying visually correct
      const origW = m.width;
      Object.defineProperty(m, 'width', { get: () => origW + (_FONT_SEED % 5) * 0.0001 });
      return m;
    };
  } catch (_) {}

  // ── 22. Object.defineProperty guard — prevent fingerprint detection bypass
  // Some detectors try to check if properties were overridden
  try {
    const _origODP = Object.defineProperty;
    // Ensure toString on overridden getters looks native
    const _NT = Function.prototype.toString;
    Function.prototype.toString = function() {
      if (this === Function.prototype.toString) return 'function toString() { [native code] }';
      const s = _NT.call(this);
      if (s.includes('native code')) return s;
      // For our overrides, return native-looking string
      if (this._xio_native) return `function ${this.name || ''}() { [native code] }`;
      return s;
    };
  } catch (_) {}

})();
"""

STEALTH_JS = _STEALTH_JS


def get_stealth_js(
    fingerprint: dict[str, Any] | None = None,
    *,
    timezone: str | None = None,
    locale: str | None = None,
    canvas_seed: int | None = None,
    audio_seed: int | None = None,
    lat: float | None = None,
    lon: float | None = None,
    ua: str | None = None,
    tz_offset: int | None = None,
    cores: int | str | None = None,
    ram: int | str | None = None,
    platform: str | None = None,
    ch_platform: str | None = None,
    ch_arch: str | None = None,
    is_mobile: bool | str | None = None,
    screen_w: int | str | None = None,
    screen_h: int | str | None = None,
    screen_ah: int | str | None = None,
    webgl_vendor: str | None = None,
    webgl_renderer: str | None = None,
) -> str:
    """Return fully-rendered stealth init script.

    Takes values from `fingerprint` dict if provided, falling back to
    individual keyword arguments or realistic defaults.
    """
    fp = fingerprint or {}

    # WebGL resolution
    _webgl_r = (
        webgl_renderer
        or fp.get("webgl_renderer")
        or "ANGLE (Apple, Apple M1, OpenGL 4.1)"
    )
    if webgl_vendor:
        _webgl_v = webgl_vendor
    elif "webgl_vendor" in fp:
        _webgl_v = fp["webgl_vendor"]
    else:
        _webgl_v = (
            "Apple"
            if "Apple" in _webgl_r
            else "Google Inc. (NVIDIA)"
            if "NVIDIA" in _webgl_r
            else "Google Inc. (Intel)"
        )

    # Timezone & offset resolution
    _tz = timezone or fp.get("timezone", "America/New_York")
    if tz_offset is not None:
        _tz_off = tz_offset
    elif "tz_offset" in fp:
        _tz_off = fp["tz_offset"]
    else:
        try:
            _tz_off = -int(
                datetime.datetime.now(zoneinfo.ZoneInfo(_tz)).utcoffset().total_seconds() // 60
            )
        except Exception:
            _tz_off = 300

    # Locale & Language
    _loc = locale or fp.get("locale", "en-US")
    _lang = _loc.split("-")[0] if "-" in _loc else _loc

    # UA and platform
    _ua_str = ua or fp.get("ua_template") or fp.get("ua") or _STEALTH_UA_CHROME
    _plat = platform or fp.get("platform", "Win32")
    _ch_plat = ch_platform or fp.get("ch_platform", "macOS")
    _ch_ar = ch_arch or fp.get("ch_arch", "arm")

    # Seeds and hardware
    _c_seed = canvas_seed if canvas_seed is not None else fp.get("canvas_seed", 42)
    _a_seed = audio_seed if audio_seed is not None else fp.get("audio_seed", 7)
    _hw_cores = str(cores if cores is not None else fp.get("cores") or fp.get("hardware_concurrency", 8))
    _hw_ram = str(ram if ram is not None else fp.get("ram") or fp.get("device_memory", 16))

    # Screen dimensions
    _w = str(screen_w if screen_w is not None else fp.get("width") or fp.get("screen_width", 1920))
    _h = str(screen_h if screen_h is not None else fp.get("height") or fp.get("screen_height", 1080))
    _ah = str(screen_ah if screen_ah is not None else fp.get("screen_ah") or _h)

    # Geolocation
    _latitude = str(lat if lat is not None else fp.get("lat", 40.7128))
    _longitude = str(lon if lon is not None else fp.get("lon", -74.0060))

    # Mobile
    _mobile_val = is_mobile if is_mobile is not None else fp.get("is_mobile", False)
    _mobile_str = "true" if _mobile_val is True or _mobile_val == "true" else "false"

    rendered = (
        _STEALTH_JS.replace("{WEBGL_V}", _webgl_v)
        .replace("{WEBGL_R}", _webgl_r)
        .replace("'{UA}'", f"'{_ua_str}'")
        .replace("{UA}", _ua_str)
        .replace("{PLATFORM}", _plat)
        .replace("'{TIMEZONE}'", f"'{_tz}'")
        .replace("{TIMEZONE}", _tz)
        .replace("{TZ_OFFSET}", str(_tz_off))
        .replace("{CANVAS_SEED}", str(_c_seed))
        .replace("{AUDIO_SEED}", str(_a_seed))
        .replace("{SCREEN_W}", _w)
        .replace("{SCREEN_H}", _h)
        .replace("{SCREEN_AH}", _ah)
        .replace("{CORES}", _hw_cores)
        .replace("{RAM}", _hw_ram)
        .replace("'{LOCALE}'", f"'{_loc}'")
        .replace("{LOCALE}", _loc)
        .replace("{LANG}", _lang)
        .replace("{CH_PLATFORM}", _ch_plat)
        .replace("{CH_ARCH}", _ch_ar)
        .replace("{LAT}", _latitude)
        .replace("{LON}", _longitude)
        .replace("{IS_MOBILE}", _mobile_str)
        .replace("{UA_TEMPLATE}", _ua_str)
    )
    return rendered
