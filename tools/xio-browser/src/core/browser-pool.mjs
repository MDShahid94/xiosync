// ─── Browser Pool ─────────────────────────────────────────────────────────
// Manages a single shared Chromium instance with isolated BrowserContexts
// per session. Applies the full CDP fingerprint stack derived from the
// exit node's Tailscale peer profile (OS/arch → UA, WebGL, hardware, etc.)

import { existsSync } from 'node:fs';
import net             from 'node:net';
import { chromium }   from 'patchright';
import { createLogger } from '../utils/logger.mjs';
import { buildProfileFromPeer, ensureDaemonRunning } from '../utils/tailscale.mjs';
import { loadStorageState, localProfilePath, restoreProfile, ensureSessionState, saveStorageState } from './session-manager.mjs';

const log = createLogger('browser-pool');

let _browser = null;
let _browserPromise = null;

// Active contexts keyed by session ID
const _contexts = new Map();
const _contextPromises = new Map();
// Last-used timestamp per session (for idle eviction)
const _contextLastUsed = new Map();

// ── Browser Lifecycle ──────────────────────────────────────────────────────

export async function getBrowser() {
  if (_browser && _browser.isConnected()) return _browser;
  if (_browserPromise) return _browserPromise;

  _browserPromise = (async () => {
    try {
      // Use Xvfb virtual display (headless:false) when available — significantly
      // harder for Google to detect than true headless Chrome.
      // Colab boot.py starts Xvfb on :1 and sets DISPLAY=:1.
      const useHeadless = !process.env.DISPLAY;
      log.info(`Launching Chromium (headless=${useHeadless}, DISPLAY=${process.env.DISPLAY || 'not set'})`);

      const b = await chromium.launch({
        headless: useHeadless,
        chromiumSandbox: false,      // REQUIRED: Colab runs as root; patchright needs this explicitly
        // ⚠️  Do NOT set executablePath — patchright must use its own patched Chromium
        // binary. Using system google-chrome breaks patchright's CDP pipe mechanism.
        args: [
          '--no-sandbox',              // Required in Colab (running as root)
          '--disable-setuid-sandbox',  // Belt + braces for sandboxing in container
          '--disable-dev-shm-usage',
          '--use-gl=angle',
          '--use-angle=swiftshader',        // Software WebGL (Colab has no GPU)
          '--disable-gpu-sandbox',
          '--ignore-gpu-blocklist',
          // ── Anti-detection: hide automation signals ──────────────────────────
          '--disable-blink-features=AutomationControlled', // hides navigator.webdriver
          '--disable-infobars',             // removes "Chrome is being controlled" bar
          '--disable-features=ServiceWorker',  // UserAgentClientHint intentionally ENABLED — disabling it leaks undefined UACH (bot signal)
          '--disable-service-workers',
          '--window-size=1920,1080',        // match Xvfb screen resolution
          '--start-maximized',
          // ── Remote DevTools (accessible via SSH tunnel from Mac) ───────────
          '--remote-debugging-port=9222',
          '--remote-debugging-address=0.0.0.0', // bind to Tailscale IP as well
        ],
      });

      b.on('disconnected', () => {
        log.warn('Browser disconnected — emergency-saving all active context states before clearing');
        // Best-effort: snapshot every live context's cookies before clearing the map.
        // IMPORTANT: We always pass a forced mid-auth URL so saveStorageState's mid-auth guard
        // skips Google cookie updates. When a job is killed (e.g. during Colab OAuth consent),
        // Google may have already rotated SIDCC with the same TTL — undetectable by TTL comparison.
        // Emergency saves should NEVER overwrite Google cookies; only non-emergency saves (at step
        // completion, with verified session state) are trusted to update them.
        const EMERGENCY_GUARD_URL = 'https://accounts.google.com/o/oauth2/emergency-save';
        const snapshots = [];
        for (const [sid, entry] of _contexts.entries()) {
          snapshots.push(
            entry.context.storageState()
              .then(st => saveStorageState(sid, st, EMERGENCY_GUARD_URL))
              .catch(() => {})
          );
        }
        Promise.allSettled(snapshots).finally(() => {
          _browser  = null;
          _contexts.clear();
          _contextLastUsed.clear();
          log.info('Browser disconnected — context map cleared after emergency save');
        });
      });

      _browser = b;
      return b;
    } finally {
      _browserPromise = null;
    }
  })();

  return _browserPromise;
}


// ── Context Management ─────────────────────────────────────────────────────

/**
 * Get or create a BrowserContext for a session.
 * If a context already exists for this session it is reused unless opts.forceNew is set.
 *
 * @param {string} sessionId    - session identifier
 * @param {string} exitNodeIP   - Tailscale IP of the exit node for fingerprinting
 * @param {object} [opts]
 * @param {boolean} [opts.forceNew=false] - Close & evict existing context, create a fresh one.
 *   Use when the cached context is known-bad (e.g. after a newPage() failure).
 */
export async function getContext(sessionId, exitNodeIP, opts = {}) {
  const { forceNew = false } = opts;

  // Evict stale cached context when forceNew is requested
  if (forceNew && _contexts.has(sessionId)) {
    log.warn(`[browser-pool] forceNew=true — evicting existing context for ${sessionId}`);
    const stale = _contexts.get(sessionId);
    await stale.context.close().catch(() => {});
    _contexts.delete(sessionId);
    _contextLastUsed.delete(sessionId);
  }

  if (!forceNew && _contexts.has(sessionId)) {
    _contextLastUsed.set(sessionId, Date.now());
    return _contexts.get(sessionId);
  }

  if (!forceNew && _contextPromises.has(sessionId)) {
    return _contextPromises.get(sessionId);
  }

  const promise = (async () => {
    // Ensure Tailscale is healthy before building fingerprint
    ensureDaemonRunning();

    // On-demand sync from Drive for this session
    await ensureSessionState(sessionId);

    const profile    = buildProfileFromPeer(exitNodeIP);
    const browser    = await getBrowser();
    const profileDir = restoreProfile(sessionId);
    const storage    = loadStorageState(sessionId);

  log.info(`Creating context for ${sessionId} (exit=${exitNodeIP}, os=${profile.chPlatform})`);

  let socks5Ok = await isSocks5Available();

  if (!socks5Ok && exitNodeIP) {
    // Tailscale kernel mode routes traffic natively without SOCKS5 — retry before assuming failure
    log.warn('SOCKS5 not ready — retrying for up to 20s (normal on fresh boot)...');
    for (let i = 0; i < 10; i++) {
      await new Promise(r => setTimeout(r, 2000));
      socks5Ok = await isSocks5Available();
      if (socks5Ok) { log.info('SOCKS5 ready after retry'); break; }
    }
    if (!socks5Ok) {
      log.warn('SOCKS5 unavailable after 20s — proceeding without proxy (kernel-mode Tailscale routes natively)');
    }
  }

  const contextOpts = {
    // NOTE: userDataDir is NOT a valid newContext() option (that's launchPersistentContext).
    // Session state is injected post-creation via loadAndHydrateContext() below,
    // which handles cookies + localStorage + IndexedDB from the v2 session file.
    ...(socks5Ok ? { proxy: { server: 'socks5://127.0.0.1:1055' } } : {}),
    userAgent:     profile.userAgent,
    viewport:      { width: profile.width, height: profile.height },
    locale:        'en-US',
    timezoneId:    await resolveTimezone(exitNodeIP),
  };

  let context = await browser.newContext(contextOpts);
  await applyFingerprintOverrides(context, profile);

  // ── Post-creation hydration (v2 soft-persistence) ─────────────────────────
  // loadAndHydrateContext injects all session state from the JSON file:
  //   Step 1 — context.addCookies() for all cookies (incl. httpOnly)
  //   Step 2 — localStorage injection per saved origin
  //   Step 3 — IndexedDB injection per saved origin
  //   Step 4 — Warm-up navigation to trigger Google rotating cookie renewal
  // Falls back silently if no session file exists (blank context).
  if (storage) {
    try {
      const { loadAndHydrateContext } = await import('./session-manager.mjs');
      await loadAndHydrateContext(sessionId, context);

      // ── Context health check after hydration ───────────────────────────
      // The warm-up navigation in Step 4 can destabilize the context when
      // Google session cookies are expired (Google redirects to a challenge
      // page whose JS response path closes the renderer). Verify the context
      // is still usable before caching it.
      const _testPage = await context.newPage().catch(() => null);
      if (_testPage) {
        await _testPage.close().catch(() => {});
        // Context alive — all good.
      } else {
        // Context died during hydration (expired session warm-up crash).
        // Recover: create a fresh context and inject cookies only (no navigation).
        log.warn(`[browser-pool] Context died during hydration for ${sessionId} — recovering with cookies-only fallback`);
        await context.close().catch(() => {});
        context = await browser.newContext(contextOpts);
        await applyFingerprintOverrides(context, profile);
        if (storage.cookies?.length > 0) {
          const nowSec = Date.now() / 1000;
          const validCookies = storage.cookies.filter(c =>
            (c.expires ?? -1) <= 0 || c.expires > nowSec
          ).map(c => ({
            name:     c.name,
            value:    c.value,
            domain:   c.domain,
            path:     c.path ?? '/',
            expires:  (c.expires ?? -1) <= 0 ? undefined : c.expires,
            httpOnly: c.httpOnly ?? false,
            secure:   c.secure ?? false,
            sameSite: c.sameSite === 'None' ? 'None' : c.sameSite === 'Strict' ? 'Strict' : 'Lax',
          }));
          try {
            await context.addCookies(validCookies);
            log.info(`[browser-pool] Recovery: injected ${validCookies.length} cookies (no warm-up) for ${sessionId}`);
          } catch (ce) {
            log.warn(`[browser-pool] Recovery addCookies failed: ${ce.message}`);
          }
        }
      }
    } catch (hydErr) {
      // Hydration failure is non-fatal: context works but may need re-login
      log.warn(`getContext: hydration failed for ${sessionId} (${hydErr.message}) — proceeding with blank context`);
      // Fallback: inject cookies directly at minimum
      if (storage.cookies?.length > 0) {
        try {
          await context.addCookies(storage.cookies);
          log.info(`getContext: fallback addCookies: ${storage.cookies.length} cookies for ${sessionId}`);
        } catch (ce) {
          log.warn(`getContext: fallback addCookies also failed: ${ce.message}`);
        }
      }
    }
  }

  _contexts.set(sessionId, { context, profile, exitNodeIP });
  _contextLastUsed.set(sessionId, Date.now());
  return { context, profile, exitNodeIP };
  })();

  _contextPromises.set(sessionId, promise);
  try {
    return await promise;
  } finally {
    _contextPromises.delete(sessionId);
  }
}

export async function closeContext(sessionId) {
  const entry = _contexts.get(sessionId);
  if (entry) {
    // Save state before closing so idle-eviction doesn't lose accumulated cookies.
    // Merge-save: only upgrades TTL — never degrades the stored session.
    // pageUrl is passed so the mid-auth guard can block saving degraded redirect tokens.
    try {
      const _pages  = entry.context.pages?.() ?? [];
      const pageUrl = _pages[0]?.url?.() ?? null;
      const st = await entry.context.storageState();
      await saveStorageState(sessionId, st, pageUrl);
    } catch (saveErr) {
      log.warn(`closeContext: pre-close state save failed for ${sessionId} (non-fatal): ${saveErr.message}`);
    }
    await entry.context.close().catch(() => {});
    _contexts.delete(sessionId);
    _contextLastUsed.delete(sessionId);
    log.info(`Context closed: ${sessionId}`);
  }
}

/**
 * Evict BrowserContexts that have been idle for longer than maxIdleMs.
 * Called by job-manager after each job to prevent Chromium OOM over time.
 */
export async function evictIdleContexts(maxIdleMs = 30 * 60 * 1000) {
  const now = Date.now();
  for (const [sessionId, lastUsed] of _contextLastUsed.entries()) {
    if (now - lastUsed > maxIdleMs) {
      await closeContext(sessionId);
      log.info(`Evicted idle context: ${sessionId} (idle ${Math.round((now - lastUsed) / 60000)}m)`);
    }
  }
}

export function listActiveContexts() {
  return [..._contexts.keys()];
}

// ── Fingerprint CDP Overrides ──────────────────────────────────────────────
// Applied to every new page in the context. Matches Colab_Antibot_Browser.ipynb.

const _cdpSessions = new WeakMap();

async function applyFingerprintOverrides(context, p) {
  await context.addInitScript(`
    (() => {
      const makeNative = (fn, name) => {
        Object.defineProperty(fn, 'name', { value: name, configurable: true });
        const s = "function " + name + "() { [native code] }";
        fn.toString = () => s;
      };

      // ── 1. navigator.webdriver — CRITICAL: Google checks this first ──────────
      Object.defineProperty(Navigator.prototype, 'webdriver',
        { get: () => undefined, configurable: true });

      // ── 2. Core navigator overrides ───────────────────────────────────
      Object.defineProperty(Navigator.prototype, 'platform',
        { get: () => '${p.platform}', configurable: true });
      Object.defineProperty(Navigator.prototype, 'hardwareConcurrency',
        { get: () => ${p.cores}, configurable: true });
      Object.defineProperty(Navigator.prototype, 'deviceMemory',
        { get: () => ${p.ram}, configurable: true });
      Object.defineProperty(Navigator.prototype, 'languages',
        { get: () => ['en-US', 'en'], configurable: true });

      // ── 3. navigator.plugins (empty plugins = headless, Google flags this) ───
      const _fakePl = [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
        { name: 'Native Client',     filename: 'internal-nacl-plugin',             description: '' },
      ];
      Object.defineProperty(Navigator.prototype, 'plugins',
        { get: () => Object.assign(_fakePl, { length: _fakePl.length, item: (i) => _fakePl[i], namedItem: (n) => _fakePl.find(p=>p.name===n)||null, refresh: ()=>{} }),
          configurable: true });
      Object.defineProperty(Navigator.prototype, 'mimeTypes',
        { get: () => Object.assign([], { length: 0, item: ()=>null, namedItem: ()=>null }),
          configurable: true });

      // ── 4. window.chrome (headless Chrome omits this — major tell) ────────
      window.chrome = {
        app: { isInstalled: false },
        runtime: {
          OnInstalledReason: { CHROME_UPDATE:'chrome_update', INSTALL:'install', UPDATE:'update' },
          PlatformOs: { MAC:'mac', WIN:'win', LINUX:'linux', ANDROID:'android', CROS:'cros' },
          connect: () => {}, sendMessage: () => {}
        },
        loadTimes: function() { return { firstPaintTime: performance.now()/1000, firstPaintAfterLoadTime: 0, requestTime: performance.timing.requestStart/1000 }; },
        csi: function() { return { startE: performance.timing.navigationStart, onloadT: performance.timing.loadEventEnd, pageT: performance.now(), tran: 15 }; }
      };
      makeNative(window.chrome.loadTimes, 'loadTimes');
      makeNative(window.chrome.csi, 'csi');

      // ── 5. Remove cdc_ automation artifacts (undetected-chromedriver residue) ─
      for (let key in window) {
        if (key.startsWith('cdc_')) {
          try { delete window[key]; } catch(_) {}
        }
      }

      // ── 6. Unified WebGL getParameter spoof ───────────────────────────────
      // Single definition covering both vendor/renderer AND secondary SwiftShader
      // params. Two separate defineProperty() calls caused fragile ordering issues.
      const _patchWebGL = (ctxName) => {
        if (!window[ctxName]) return;
        const origGetParam = window[ctxName].prototype.getParameter;
        const spoofGetParam = function(param) {
          if (param === 37445)  return '${p.webglVendor}';   // UNMASKED_VENDOR_WEBGL
          if (param === 37446)  return '${p.webglRenderer}'; // UNMASKED_RENDERER_WEBGL
          if (param === 0x0D33) return 16384;   // MAX_TEXTURE_SIZE (SwiftShader=4096)
          if (param === 0x0D3A) return [32767, 32767]; // MAX_VIEWPORT_DIMS
          if (param === 0x84E8) return 16384;   // MAX_RENDERBUFFER_SIZE
          if (param === 0x0B4D) return [1, 1];  // ALIASED_LINE_WIDTH_RANGE
          if (param === 0x846E) return [1, 1];  // ALIASED_POINT_SIZE_RANGE
          if (param === 0x8B8C) return 'WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)';
          if (param === 0x1F02) return 'WebGL 1.0 (OpenGL ES 2.0 Chromium)';
          return origGetParam.apply(this, arguments);
        };
        Object.defineProperty(window[ctxName].prototype, 'getParameter',
          { value: spoofGetParam, writable: true, configurable: true, enumerable: false });
        makeNative(spoofGetParam, 'getParameter');
      };
      ['WebGLRenderingContext', 'WebGL2RenderingContext'].forEach(_patchWebGL);

      // OffscreenCanvas WebGL (web workers use this for GPU fingerprinting)
      if (window.OffscreenCanvas) {
        const _origGetCtx = OffscreenCanvas.prototype.getContext;
        OffscreenCanvas.prototype.getContext = function(type, attrs) {
          const ctx = _origGetCtx.apply(this, arguments);
          if (ctx && (type === 'webgl' || type === 'webgl2')) {
            const _origParam = ctx.getParameter;
            ctx.getParameter = function(p) {
              if (p === 37445) return '${p.webglVendor}';
              if (p === 37446) return '${p.webglRenderer}';
              return _origParam.apply(this, arguments);
            };
          }
          return ctx;
        };
      }

      // ── 7. Media devices fingerprint ──────────────────────────────────
      if (navigator.mediaDevices) {
        const _fakeDevices = [
          { kind: 'videoinput',  deviceId: 'cam0', label: '${p.camName}', groupId: 'g1' },
          { kind: 'audioinput',  deviceId: 'mic0', label: 'Internal Microphone', groupId: 'g2' },
          { kind: 'audiooutput', deviceId: 'spk0', label: 'Internal Speaker',    groupId: 'g2' },
        ];
        navigator.mediaDevices.enumerateDevices = () => Promise.resolve(_fakeDevices);
        makeNative(navigator.mediaDevices.enumerateDevices, 'enumerateDevices');
      }

      // ── 8. WebRTC FULL KILL (B3) — prevents local IP leak via ICE candidates ─
      // Replace iceServers=[] (partial, exploitable) with full object removal.
      // createOffer({iceTransportPolicy:'all'}) still leaks with iceServers=[] alone.
      ['RTCPeerConnection','webkitRTCPeerConnection','mozRTCPeerConnection',
       'RTCIceCandidate','RTCSessionDescription'].forEach(k => {
        try {
          Object.defineProperty(window, k, { get: () => undefined, configurable: false });
        } catch (_) {}
      });
      // Also override mediaDevices fully for complete device isolation
      try {
        Object.defineProperty(navigator, 'mediaDevices', {
          get: () => ({
            getUserMedia: () => Promise.reject(
              Object.assign(new DOMException('Permission denied'), { name: 'NotAllowedError' })
            ),
            enumerateDevices: () => Promise.resolve([
              { kind: 'audioinput',  deviceId: 'default', groupId: 'default', label: '' },
              { kind: 'audiooutput', deviceId: 'default', groupId: 'default', label: '' },
              { kind: 'videoinput',  deviceId: 'cam0',    groupId: 'cam0',
                label: '${p.camName || "FaceTime HD Camera"}' },
            ]),
            getSupportedConstraints: () => ({
              width: true, height: true, frameRate: true,
              aspectRatio: true, facingMode: true,
            }),
          }),
          configurable: false,
        });
      } catch (_) {}

      // ── 9b. Screen dimensions (B2) — matches Xvfb resolution + profile ────
      try {
        Object.defineProperties(window.screen, {
          width:       { get: () => ${p.width || 1920},        configurable: true },
          height:      { get: () => ${p.height || 1080},       configurable: true },
          availWidth:  { get: () => ${p.width || 1920},        configurable: true },
          availHeight: { get: () => ${(p.height || 1080) - 40}, configurable: true },
          colorDepth:  { get: () => 30,                        configurable: true },
        });
        Object.defineProperty(window, 'devicePixelRatio', {
          get: () => ${p.dpr || 1},
          configurable: true,
        });
      } catch (_) {}

      // ── 9c. Canvas noise (B4) — stable per-slot seed, imperceptible ────────
      (function() {
        const SEED = ${p.canvasSeed || 0};
        if (!SEED) return; // seed=0 → no noise (Linux honest profile)
        const N = ((SEED * 0x9e3779b9) >>> 0);
        const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
          const data = _origToDataURL.call(this, type, quality);
          if (!data || this.width === 0 || this.height === 0) return data;
          // Stable 1-byte perturbation at fixed position (imperceptible to humans)
          const pos = data.length - 4;
          return pos > 0
            ? data.slice(0, pos) +
              String.fromCharCode((data.charCodeAt(pos) ^ (N & 0x1F)) || data.charCodeAt(pos)) +
              data.slice(pos + 1)
            : data;
        };
        const _origGID = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = function(...a) {
          const d = _origGID.apply(this, a);
          if (d.data.length > 0) d.data[0] = (d.data[0] + (N & 0x03)) & 0xFF;
          return d;
        };
      })();

      // ── 9d. Audio noise (B5) — stable per-slot seed ────────────────────────
      (function() {
        const SEED = ${p.audioSeed || 0};
        if (!SEED) return;
        const _origGCD = AudioBuffer.prototype.getChannelData;
        AudioBuffer.prototype.getChannelData = function(ch) {
          const arr = _origGCD.call(this, ch);
          // Imperceptible stable perturbation: < 1e-12 amplitude, human threshold ~1e-3
          arr[0] = arr[0] + ((SEED ^ (ch * 0x1234)) % 1000) * 1e-14;
          return arr;
        };
      })();



      // ── 9. Web Worker and Shared Worker spoofing ──────────────────────────
      const resolveURL = (url) => {
        try { return new URL(url, window.location.href).href; }
        catch (e) { return url; }
      };

      const workerPayload = 
        "if (self.WorkerNavigator) {\\n" +
        "  const proto = self.WorkerNavigator.prototype;\\n" +
        "  Object.defineProperty(proto, 'userAgent', { get: () => '${p.userAgent}', configurable: true, enumerable: true });\\n" +
        "  Object.defineProperty(proto, 'platform', { get: () => '${p.platform}', configurable: true, enumerable: true });\\n" +
        "  Object.defineProperty(proto, 'hardwareConcurrency', { get: () => ${p.cores}, configurable: true, enumerable: true });\\n" +
        "  Object.defineProperty(proto, 'deviceMemory', { get: () => ${p.ram}, configurable: true, enumerable: true });\\n" +
        "  Object.defineProperty(proto, 'userAgentData', {\\n" +
        "    get: () => ({\\n" +
        "      brands: [\\n" +
        "        { brand: 'Chromium', version: '${String(p.cvMajor || 120)}' },\\n" +
        "        { brand: 'Google Chrome', version: '${String(p.cvMajor || 120)}' }\\n" +
        "      ],\\n" +
        "      mobile: ${p.isMobile ? true : false},\\n" +
        "      platform: '${p.chPlatform || 'Linux'}',\\n" +
        "      getHighEntropyValues: async () => ({\\n" +
        "        architecture: '${p.chArch || 'x86'}',\\n" +
        "        bitness: '64',\\n" +
        "        model: '${p.isMobile ? 'SM-S901B' : ''}',\\n" +
        "        platform: '${p.chPlatform || 'Linux'}',\\n" +
        "        platformVersion: '${p.chVersion || '5.15.0'}'\\n" +
        "      })\\n" +
        "    }),\\n" +
        "    configurable: true, enumerable: true\\n" +
        "  });\\n" +
        "}\\n" +
        "['WebGLRenderingContext', 'WebGL2RenderingContext'].forEach(function(ctxName) {\\n" +
        "  if (self[ctxName]) {\\n" +
        "    const origGetParam = self[ctxName].prototype.getParameter;\\n" +
        "    self[ctxName].prototype.getParameter = function(param) {\\n" +
        "      const res = origGetParam.apply(this, arguments);\\n" +
        "      if (param === 37445) return '${p.webglVendor}';\\n" +
        "      if (param === 37446) return '${p.webglRenderer}';\\n" +
        "      return res;\\n" +
        "    };\\n" +
        "  }\\n" +
        "});\\n" +
        "if (self.OffscreenCanvas) {\\n" +
        "  const origGetContext = self.OffscreenCanvas.prototype.getContext;\\n" +
        "  self.OffscreenCanvas.prototype.getContext = function(type, attributes) {\\n" +
        "    const ctx = origGetContext.apply(this, arguments);\\n" +
        "    if (ctx && (type === 'webgl' || type === 'webgl2')) {\\n" +
        "      const origGetParam = ctx.getParameter;\\n" +
        "      ctx.getParameter = function(param) {\\n" +
        "        const res = origGetParam.apply(this, arguments);\\n" +
        "        if (param === 37445) return '${p.webglVendor}';\\n" +
        "        if (param === 37446) return '${p.webglRenderer}';\\n" +
        "        return res;\\n" +
        "      };\\n" +
        "    }\\n" +
        "    return ctx;\\n" +
        "  };\\n" +
        "}\\n";

      const OriginalWorker = window.Worker;
      if (OriginalWorker) {
        window.Worker = function(scriptURL, options) {
          // Skip blob-wrapping for ES module workers — importScripts() is not available
          // in module context. Attempting it causes a DOMException and breaks the page.
          if (options && options.type === 'module') {
            return new OriginalWorker(scriptURL, options);
          }
          const resolved = resolveURL(scriptURL);
          const originUrl = window.location.href;
          const originHost = window.location.host;
          const originHostname = window.location.hostname;
          const locPayload = "Object.defineProperty(self, 'location', { get: () => ({ href: '" + originUrl + "', protocol: 'https:', host: '" + originHost + "', hostname: '" + originHostname + "', pathname: '/', search: '', hash: '' }), configurable: true });";
          const rawScript = locPayload + "\\n" + workerPayload + "\\nimportScripts('" + resolved + "');";
          const blob = new Blob([rawScript], { type: 'application/javascript' });
          return new OriginalWorker(URL.createObjectURL(blob), options);
        };
        makeNative(window.Worker, 'Worker');
      }

      const OriginalSharedWorker = window.SharedWorker;
      if (OriginalSharedWorker) {
        window.SharedWorker = function(scriptURL, options) {
          const resolved = resolveURL(scriptURL);
          const originUrl = window.location.href;
          const originHost = window.location.host;
          const originHostname = window.location.hostname;
          const locPayload = "Object.defineProperty(self, 'location', { get: () => ({ href: '" + originUrl + "', protocol: 'https:', host: '" + originHost + "', hostname: '" + originHostname + "', pathname: '/', search: '', hash: '' }), configurable: true });";
          const rawScript = locPayload + "\\n" + workerPayload + "\\nimportScripts('" + resolved + "');";
          const blob = new Blob([rawScript], { type: 'application/javascript' });
          return new OriginalSharedWorker(URL.createObjectURL(blob), options);
        };
        makeNative(window.SharedWorker, 'SharedWorker');
      }

    })();
  `);

  // ── Apply CDP overrides (UserAgentClientHints + Mobile Emulation) ───────────
  const applyCdp = async (page) => {
    try {
      const client = await context.newCDPSession(page);
      _cdpSessions.set(page, client);
      page.on('close', () => {
        try { client.detach().catch(() => {}); } catch {}
        _cdpSessions.delete(page);
      });
      
      const ua_metadata = {
        brands: [
          { brand: 'Chromium', version: String(p.cvMajor || '120') },
          { brand: 'Google Chrome', version: String(p.cvMajor || '120') },
          { brand: 'Not-A.Brand', version: '99' }
        ],
        fullVersionList: [
          { brand: 'Chromium', version: String(p.cvFull || '120.0.0.0') },
          { brand: 'Google Chrome', version: String(p.cvFull || '120.0.0.0') },
          { brand: 'Not-A.Brand', version: '99.0.0.0' }
        ],
        platform: p.chPlatform || 'Linux',
        platformVersion: p.chVersion || '5.15.0',
        architecture: p.chArch || 'x86',
        model: p.isMobile ? 'SM-S901B' : '',
        mobile: !!p.isMobile,
        bitness: '64',
        wow64: false
      };

      await client.send('Network.setUserAgentOverride', {
        userAgent: p.userAgent,
        platform: p.platform || 'Linux',
        acceptLanguage: 'en-US,en',
        userAgentMetadata: ua_metadata
      });

      if (p.isMobile) {
        await client.send('Emulation.setTouchEmulationEnabled', {
          enabled: true,
          maxTouchPoints: 5
        });
        await client.send('Emulation.setEmitTouchEventsForMouse', {
          enabled: true,
          configuration: 'mobile'
        });
      }
    } catch (e) {
      log.warn(`Failed to apply CDP overrides: ${e.message}`);
    }
  };

  context.on('page', applyCdp);
  await Promise.all(context.pages().map(applyCdp));
}

// ── SOCKS5 availability check ─────────────────────────────────────────────
// Returns true only if the SOCKS5 proxy is listening AND can reach the internet.
function isSocks5Available() {
  return new Promise((resolve) => {
    // Step 1: Check if port 1055 is open
    const sock = new net.Socket();
    sock.setTimeout(500);
    sock.connect(1055, '127.0.0.1', () => {
      sock.destroy();
      // Step 2: Try connecting to 8.8.8.8:53 through the SOCKS5 proxy
      // Send a minimal SOCKS5 handshake to verify routing works
      const probe = new net.Socket();
      probe.setTimeout(3000);
      probe.connect(1055, '127.0.0.1', () => {
        // SOCKS5 greeting: version=5, nmethods=1, method=0 (no auth)
        probe.write(Buffer.from([0x05, 0x01, 0x00]));
      });
      probe.once('data', (data) => {
        if (data[0] === 0x05 && data[1] === 0x00) {
          // Server accepted no-auth — send CONNECT to 8.8.8.8:53
          probe.write(Buffer.from([
            0x05, 0x01, 0x00,  // VER=5, CMD=CONNECT, RSV=0
            0x01,              // ATYP=IPv4
            8, 8, 8, 8,       // 8.8.8.8
            0x00, 0x35        // port 53
          ]));
          probe.once('data', (resp) => {
            probe.destroy();
            resolve(resp[1] === 0x00); // REP=0 means success
          });
        } else {
          probe.destroy();
          resolve(false);
        }
      });
      probe.on('error', () => resolve(false));
      probe.on('timeout', () => { probe.destroy(); resolve(false); });
    });
    sock.on('error', () => resolve(false));
    sock.on('timeout', () => { sock.destroy(); resolve(false); });
  });
}

// ── Timezone from exit node geolocation ───────────────────────────────────
// Fetches directly (no proxy dependency)

async function resolveTimezone(exitNodeIP) {
  // Tailscale IPs (100.64.0.0/10) are CGNAT — ip-api.com returns 'fail' for them.
  // After setExitNode() is called, Colab's traffic routes via the Mac exit node,
  // so querying without an IP correctly returns the Mac's home timezone.
  try {
    const resp = await fetch('http://ip-api.com/json/?fields=timezone,status', {
      signal: AbortSignal.timeout(6000)
    });
    const data = await resp.json();
    if (data.status === 'success' && data.timezone) return data.timezone;
    return 'UTC';
  } catch {
    return 'UTC';
  }
}

export async function closeBrowser() {
  if (_browser) {
    await _browser.close().catch(() => {});
    _browser = null;
    _contexts.clear();
  }
}

/**
 * Return the Playwright WebSocket endpoint for this browser.
 * Used by xb_devtools_url to expose the CDP connection for SSH tunneling.
 */
export async function getBrowserWSEndpoint() {
  const browser = await getBrowser();
  return browser.wsEndpoint();
}

/**
 * Capture a JPEG screenshot from any currently open Playwright page.
 * Priority: prefer pages with interesting URLs (accounts, google, etc.)
 * Returns a Buffer or null if no pages are open.
 */
export async function captureAnyPage() {
  if (!_browser || !_browser.isConnected()) return null;

  const allPages = [];
  for (const { context } of _contexts.values()) {
    for (const page of context.pages()) {
      if (!page.isClosed()) allPages.push(page);
    }
  }

  if (allPages.length === 0) return null;

  const score = (p) => {
    const u = p.url() || '';
    if (u.includes('accounts.google.com') || u.includes('myaccount.google.com')) return 100;
    if (u.includes('google.com')) return 80;
    if (u.startsWith('http')) return 50;
    return 0;
  };
  allPages.sort((a, b) => score(b) - score(a));

  try {
    // Stream-optimised: JPEG q55, viewport only (no full-page scroll).
    // At 4 fps this gives ~40-60 KB/s — comfortable over Tailscale at any bandwidth.
    return await allPages[0].screenshot({ type: 'jpeg', quality: 55, fullPage: false, timeout: 8000 });
  } catch {
    return null;
  }
}
