// ─── DevTools Manager ─────────────────────────────────────────────────────
// Wraps Playwright's built-in CDP session API for low-level browser control.
// All tools route through Playwright's existing connection — no extra port
// exposure or second Chrome process needed.
//
// Capabilities:
//   • screenshot  — CDP Page.captureScreenshot (higher quality than Playwright default)
//   • evaluate    — Runtime.evaluate (full V8 access, can await promises)
//   • click       — Input.dispatchMouseEvent at x,y coordinates
//   • type        — Input.dispatchKeyEvent char-by-char
//   • dom         — DOM.getOuterHTML (full page HTML snapshot)
//   • send        — raw CDP command passthrough (any DevTools Protocol method)
//   • network log — buffered Network.requestWillBeSent / responseReceived events
//   • console log — buffered Console.messageAdded events

import { createLogger } from '../utils/logger.mjs';

const log = createLogger('devtools');

// ── Per-page state ─────────────────────────────────────────────────────────
const _sessions    = new WeakMap(); // page → CDPSession
const _netLogs     = new WeakMap(); // page → request[]
const _conLogs     = new WeakMap(); // page → message[]

// ── Session Management ─────────────────────────────────────────────────────

/**
 * Get or create a CDP session for a Playwright page.
 * Automatically enables Network + Console monitoring on first access.
 *
 * @param {import('playwright-core').BrowserContext} context
 * @param {import('playwright-core').Page} page
 * @returns {Promise<import('playwright-core').CDPSession>}
 */
export async function getCDPSession(context, page) {
  if (_sessions.has(page)) return _sessions.get(page);

  log.info('Opening CDP session for page');
  const session = await context.newCDPSession(page);
  _sessions.set(page, session);

  // Enable domains for monitoring
  try { await session.send('Network.enable', { maxResourceBufferSize: 1024 * 1024 }); } catch {}
  try { await session.send('Console.enable'); } catch {}
  try { await session.send('Log.enable'); } catch {}

  // ── Buffer network events ────────────────────────────────────────────────
  const netLog = [];
  _netLogs.set(page, netLog);
  session.on('Network.requestWillBeSent', ({ requestId, request, timestamp }) => {
    netLog.push({
      id:     requestId,
      url:    request.url,
      method: request.method,
      time:   timestamp,
    });
    if (netLog.length > 300) netLog.shift();
  });
  session.on('Network.responseReceived', ({ requestId, response }) => {
    const req = netLog.findLast(r => r.id === requestId);
    if (req) { req.status = response.status; req.mime = response.mimeType; }
  });
  session.on('Network.loadingFailed', ({ requestId, errorText }) => {
    const req = netLog.findLast(r => r.id === requestId);
    if (req) req.error = errorText;
  });

  // ── Buffer console events ────────────────────────────────────────────────
  const conLog = [];
  _conLogs.set(page, conLog);
  session.on('Console.messageAdded', ({ message }) => {
    conLog.push({ level: message.level, text: message.text, url: message.url ?? '' });
    if (conLog.length > 500) conLog.shift();
  });
  session.on('Log.entryAdded', ({ entry }) => {
    conLog.push({ level: entry.level, text: entry.text, url: entry.url ?? '' });
    if (conLog.length > 500) conLog.shift();
  });

  // Detach on page close
  page.once('close', () => {
    _sessions.delete(page);
    _netLogs.delete(page);
    _conLogs.delete(page);
  });

  return session;
}

// ── CDP Command Wrapper ────────────────────────────────────────────────────

/**
 * Send any raw CDP command to the page.
 */
export async function cdpSend(context, page, method, params = {}) {
  const session = await getCDPSession(context, page);
  return session.send(method, params);
}

// ── High-Level Helpers ─────────────────────────────────────────────────────

/**
 * Take a screenshot via CDP.
 * @param {object} opts
 * @param {'png'|'jpeg'|'webp'} opts.format
 * @param {number}  opts.quality     - JPEG/WebP quality 0-100
 * @param {boolean} opts.fullPage    - capture beyond viewport
 * @param {object}  opts.clip        - { x, y, width, height, scale }
 * @returns {Promise<string>}        - base64 image data (no data: prefix)
 */
export async function cdpScreenshot(context, page, opts = {}) {
  // Default: JPEG q70 — readable for text/UI debugging, ~3-5x smaller than PNG.
  // Callers that need full fidelity can pass { format:'png' } or higher quality.
  const { format = 'jpeg', quality = 70, fullPage = false, clip } = opts;
  const session = await getCDPSession(context, page);
  const result  = await session.send('Page.captureScreenshot', {
    format,
    ...(format !== 'png' ? { quality } : {}),
    captureBeyondViewport: fullPage,
    ...(clip ? { clip } : {}),
  });
  return result.data;
}

/**
 * Evaluate a JavaScript expression in the page's main world.
 * Supports awaiting Promises.
 * @returns {Promise<*>}  - serialized return value
 */
export async function cdpEvaluate(context, page, expression, { awaitPromise = true } = {}) {
  const session = await getCDPSession(context, page);
  const result  = await session.send('Runtime.evaluate', {
    expression,
    awaitPromise,
    returnByValue:    true,
    userGesture:      true,
    includeCommandLineAPI: true,
  });
  if (result.exceptionDetails) {
    const msg = result.exceptionDetails.exception?.description
              || result.exceptionDetails.text
              || 'Unknown JS error';
    throw new Error(`[Runtime.evaluate] ${msg}`);
  }
  return result.result?.value;
}

/**
 * Dispatch a left-click at page coordinates (x, y).
 */
export async function cdpClick(context, page, x, y, { button = 'left', clickCount = 1 } = {}) {
  const session = await getCDPSession(context, page);
  for (const type of ['mousePressed', 'mouseReleased']) {
    await session.send('Input.dispatchMouseEvent', {
      type, x, y, button, clickCount,
      modifiers: 0,
      timestamp: Date.now() / 1000,
    });
  }
}

/**
 * Type text via CDP key events (works even without focus via JS click).
 */
export async function cdpType(context, page, text, { delayMs = 25 } = {}) {
  const session = await getCDPSession(context, page);
  for (const char of text) {
    await session.send('Input.dispatchKeyEvent', { type: 'char', text: char, unmodifiedText: char });
    if (delayMs > 0) await new Promise(r => setTimeout(r, delayMs));
  }
}

/**
 * Get the full outer HTML of the page's current DOM.
 */
export async function cdpGetDOM(context, page) {
  const session = await getCDPSession(context, page);
  const { root } = await session.send('DOM.getDocument', { depth: 0, pierce: false });
  const { outerHTML } = await session.send('DOM.getOuterHTML', { nodeId: root.nodeId });
  return outerHTML;
}

/**
 * Get all cookies visible to the current page.
 */
export async function cdpGetCookies(context, page) {
  const session = await getCDPSession(context, page);
  const { cookies } = await session.send('Network.getCookies', { urls: [page.url()] });
  return cookies;
}

/**
 * Return ALL cookies the browser holds — across every domain, no URL filter.
 * Uses CDP Network.getAllCookies which is the same command the uc sidecar uses
 * to extract the full Google session after login. This is the correct way to
 * capture STRP, PSIDTS, SIDCC etc. that span multiple Google subdomains.
 */
export async function cdpGetAllCookies(context, page) {
  const session = await getCDPSession(context, page);
  const { cookies } = await session.send('Network.getAllCookies');
  return cookies;
}

/**
 * Return the buffered network log for a page.
 */
export function getNetworkLog(page, { last = 50 } = {}) {
  const log = _netLogs.get(page) ?? [];
  return log.slice(-last);
}

/**
 * Return the buffered console log for a page.
 */
export function getConsoleLog(page, { last = 100 } = {}) {
  const log = _conLogs.get(page) ?? [];
  return log.slice(-last);
}
