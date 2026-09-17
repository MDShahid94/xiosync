// ─── Screencaster ─────────────────────────────────────────────────────────
// Broadcasts MJPEG frames from Chrome's Playwright virtual viewport.
//
// Capture chain:
//   1. captureAnyPage()  — screenshots the most interesting open Playwright page
//   2. job.page.screenshot() — active workflow page (already covered by 1, kept as safety)
//   3. Last good frame  — never goes completely black
//   4. Static placeholder JPEG
//
// Key design decisions:
//   • _setFps() is only called from the capture loop, not from inside capture
//   • No duplicate function declarations (prev version had infinite recursion bug)
//   • _capturing flag prevents overlapping screenshot calls

import { captureAnyPage } from './browser-pool.mjs';
import { createLogger }   from '../utils/logger.mjs';
import { registerStream, unregisterStream, setGlobalFps } from './stream-registry.mjs';

const log = createLogger('screencaster');

// ── Constants ────────────────────────────────────────────────────────────────
const BOUNDARY      = 'XIOFRAME';
const INTERVAL_JOB  = 1000 / 4;    // 4 fps — active workflow
const INTERVAL_IDLE = 2000;         // 0.5 fps — no workflow

// Compressed screenshot options for the streaming loop.
// scale:0.5 halves both dimensions (1280px Colab → 640px stream frame).
// JPEG q55 gives ~10-15 KB per frame vs ~150-300 KB for uncompressed PNG.
// This is the right trade-off: readable for debugging, fast to transfer.
const STREAM_SHOT_OPTS = { type: 'jpeg', quality: 55, fullPage: false, scale: 'css', timeout: 6000 };
// Playwright does not have a built-in scale param — we use clip trick with
// omitBackground:false; actual pixel shrink happens in captureAnyPage (browser-pool)
// which already passes type:'jpeg', quality:78. For the safety-net path here we use 55.

// ── State ────────────────────────────────────────────────────────────────────
const _clients       = new Set();
let   _captureTimer  = null;
let   _lastFrame     = null;
let   _getRunningJob = null;
let   _capturing     = false;

// ── Injected accessors ────────────────────────────────────────────────────────
export function setJobAccessor(fn)     { _getRunningJob = fn; }
export function setBrowserAccessor(fn) { /* no-op */ }

// ── Stream Registry Integration ────────────────────────────────────────────
// Register this screencaster as the 'xio-main' stream in the central registry.
// Called once during server startup from http-transport.mjs.
let _registeredInRegistry = false;

export function initStreamRegistration() {
  if (_registeredInRegistry) return;
  _registeredInRegistry = true;
  registerStream('xio-main', 'XIO Browser', 'headed', async () => {
    // Reuse the existing capture logic
    try {
      const { captureAnyPage } = await import('./browser-pool.mjs');
      const buf = await captureAnyPage();
      if (buf && buf.length > 500) return buf;
    } catch {}
    // Fallback: active job page
    const job = _getRunningJob?.();
    if (job?.page && !job.page.isClosed?.()) {
      try {
        return await job.page.screenshot({ type: 'jpeg', quality: 55, fullPage: false, timeout: 6000 });
      } catch {}
    }
    // Fallback: last frame or placeholder
    return _lastFrame ?? WAITING_JPEG;
  }, 4); // 4 fps base
  log.info('Registered xio-main stream in stream-registry');
}

// ── Client Management ─────────────────────────────────────────────────────────
export function registerClient(res) {
  res.writeHead(200, {
    'Content-Type':                `multipart/x-mixed-replace; boundary=${BOUNDARY}`,
    'Cache-Control':               'no-cache, no-store',
    'Connection':                  'keep-alive',
    'Transfer-Encoding':           'identity',
    'X-Accel-Buffering':           'no',   // disable nginx/Tailscale proxy buffering
    'Access-Control-Allow-Origin': '*',
    'X-Content-Type-Options':      'nosniff',
  });
  res.setTimeout(0);

  _clients.add(res);
  log.info(`Stream client connected — total: ${_clients.size}`);

  // Send last known frame immediately so client doesn't see blank start
  if (_lastFrame) _sendFrame(res, _lastFrame);

  // Keep-alive heartbeat so proxies don't drop silent connections
  const keepAlive = setInterval(() => {
    try { res.write(`--${BOUNDARY}\r\nContent-Type: text/plain\r\n\r\nka\r\n`); }
    catch { clearInterval(keepAlive); }
  }, 10_000);

  res.on('close', () => {
    _clients.delete(res);
    clearInterval(keepAlive);
    log.info(`Stream client disconnected — total: ${_clients.size}`);
    if (_clients.size === 0) _stopCapture();
  });

  res.on('error', (err) => {
    // Swallow ungraceful disconnects (e.g. ECONNRESET) to prevent Node.js crash
    log.warn(`Stream client error: ${err.message}`);
  });

  _startCapture();
}

// ── Frame Emission ────────────────────────────────────────────────────────────
function _sendFrame(res, buf) {
  try {
    res.write(`--${BOUNDARY}\r\n`);
    res.write(`Content-Type: image/jpeg\r\n`);
    res.write(`Content-Length: ${buf.length}\r\n\r\n`);
    res.write(buf);
    res.write('\r\n');
  } catch { /* client dropped — will be removed on 'close' */ }
}

function _broadcast(buf) {
  _lastFrame = buf;
  for (const res of _clients) _sendFrame(res, buf);
  // Notify SSE frame subscribers
  for (const [id, cb] of _frameSubscribers) {
    try { cb(buf); } catch { _frameSubscribers.delete(id); }
  }
}

// ── Capture Loop ──────────────────────────────────────────────────────────────

function _startCapture() {
  if (_captureTimer) return;           // already running
  _scheduleTick();
  log.info('Capture loop started');
}

function _stopCapture() {
  if (_captureTimer) {
    clearTimeout(_captureTimer);
    _captureTimer = null;
  }
  log.info('Capture loop stopped — no clients');
}

// Dynamic FPS override from resource monitor
let _dynamicFps = null;

export function setDynamicFps(fps) {
  _dynamicFps = fps;
  // If capture is running, restart it with the new interval
  if (_captureTimer && (_clients.size > 0 || _frameSubscribers.size > 0)) {
    clearTimeout(_captureTimer);
    _captureTimer = null;
    if (fps > 0) _scheduleTick();
  }
}

function _scheduleTick() {
  if (_dynamicFps !== null && _dynamicFps <= 0) return;  // paused by resource monitor
  const job      = _getRunningJob?.();
  let interval;
  if (_dynamicFps !== null && _dynamicFps > 0) {
    interval = Math.floor(1000 / _dynamicFps);
  } else {
    interval = job?.page ? INTERVAL_JOB : INTERVAL_IDLE;
  }
  _captureTimer  = setTimeout(_tick, interval);
}

async function _tick() {
  _captureTimer = null;
  if (_clients.size === 0 && _frameSubscribers.size === 0) return;  // nobody listening

  if (!_capturing) {
    _capturing = true;
    try {
      await _doCapture();
    } catch (e) {
      log.warn(`Capture error: ${e.message}`);
    } finally {
      _capturing = false;
    }
  }

  // Schedule next tick (even if we skipped this one due to _capturing)
  if (_clients.size > 0 || _frameSubscribers.size > 0) _scheduleTick();
}

async function _doCapture() {
  // ── 1. captureAnyPage — gets any open Playwright page ──────────────────
  try {
    const buf = await captureAnyPage();
    if (buf && buf.length > 500) {
      _broadcast(buf);
      return;
    }
  } catch { /* browser not ready yet */ }

  // ── 2. Active workflow page (safety net) ─────────────────────────────────
  const job = _getRunningJob?.();
  if (job?.page && !job.page.isClosed?.()) {
    try {
      const buf = await job.page.screenshot(
        // Compressed: JPEG q55, no full-page — fast enough for 4 fps
        { type: 'jpeg', quality: 55, fullPage: false, timeout: 6000 },
      );
      _broadcast(buf);
      return;
    } catch (e) {
      log.warn(`job.page screenshot: ${e.message.split('\n')[0]}`);
    }
  }

  // ── 3. Re-broadcast last good frame — never go black ─────────────────────
  if (_lastFrame) {
    _broadcast(_lastFrame);
  } else {
    _broadcast(WAITING_JPEG);
  }
}

// ── Static waiting placeholder ────────────────────────────────────────────────
const WAITING_JPEG = Buffer.from(
  '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8U' +
  'HRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAAgACABAREA' +
  'Ax8A/8QAFAABAAAAAAAAAAAAAAAAAAAACP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEA' +
  'AT8AVgAB/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwBX/8QAFBEBAAAAAAAAAAAAA' +
  'AAAAAAAP/aAAgBAwEBPwBX/9k=',
  'base64'
);

// ── Public API ─────────────────────────────────────────────────────────────
export function getStreamInfo(port, tsIP) {
  return {
    stream_url:     `http://${tsIP}:${port}/stream`,
    stream_sse_url: `http://${tsIP}:${port}/stream/sse`,
    stream_view_url:`http://${tsIP}:${port}/stream/sse/view`,
    dynamic_fps:    _dynamicFps,
    clients:    _clients.size,
    capturing:  !!_captureTimer || _capturing,
    fps:        _getRunningJob?.()?.page ? 4 : 0.5,
    source:     'playwright-captureAnyPage',
  };
}

// ── Frame Subscriber API (for SSE /stream/sse endpoint) ──────────────────────
// Allows non-MJPEG consumers (SSE, WebSocket) to receive raw JPEG buffers.
const _frameSubscribers = new Map();
let   _subIdCounter = 0;

export function subscribeFrames(callback) {
  const id = ++_subIdCounter;
  _frameSubscribers.set(id, callback);
  // Start capture if not already running
  _startCapture();
  // Send last frame immediately
  if (_lastFrame) { try { callback(_lastFrame); } catch {} }
  return id;
}

export function unsubscribeFrames(id) {
  _frameSubscribers.delete(id);
  // Stop capture if nobody listening
  if (_clients.size === 0 && _frameSubscribers.size === 0) _stopCapture();
}
