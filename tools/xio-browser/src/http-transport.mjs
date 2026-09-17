// ─── HTTP Transport: Streamable HTTP (proxy-safe) ────────────────────────────
// Singleton McpServer reused across all requests; only the
// StreamableHTTPServerTransport is fresh per POST /mcp. This avoids the cost
// of re-registering 35+ tools on every request while keeping transport state
// stateless. Job/session state persists in SQLite so clients can poll across
// independent requests. Works through any HTTPS proxy (tailscale serve, nginx).
//
// Extra endpoints:
//   GET /health          — liveness probe
//   GET /stream          — MJPEG live stream of the active browser page
//   GET /devtools        — JSON: CDP targets + stream URL (for Mac auto-connect)
//   GET /devtools/bridge — WebSocket proxy to Colab Chrome's CDP (port 9222)

import express    from 'express';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { createLogger }   from './utils/logger.mjs';
import { registerClient, setJobAccessor, getStreamInfo, initStreamRegistration } from './core/screencaster.mjs';
import { listStreams, getStream, registerMjpegClient, registerSseClient,
         pushFrame, setPrimaryStream, registerStream, unregisterStream } from './core/stream-registry.mjs';
import { startMonitor, getResourceSnapshot } from './core/resource-monitor.mjs';
import { setGlobalFps } from './core/stream-registry.mjs';
import { selfIP }         from './utils/tailscale.mjs';
import { Paths }          from './utils/drive.mjs';
import fs                 from 'node:fs';
import path               from 'node:path';

const log = createLogger('http');

// serverFactory: async () => McpServer instance (called per-request)
export async function startHttpServer(serverFactory, port, { getRunningJob } = {}) {
  // Wire up accessors so screencaster can reach the live page and render status cards
  if (getRunningJob) setJobAccessor(getRunningJob);
  // Note: setBrowserAccessor is a no-op in screencaster.mjs — captureAnyPage() in
  // browser-pool.mjs is imported directly by screencaster, so no accessor needed.

  // ── Initialize stream registry + resource monitor ───────────────────────
  try { initStreamRegistration(); } catch (e) { log.warn(`Stream registry init: ${e.message}`); }
  try { startMonitor(setGlobalFps); } catch (e) { log.warn(`Resource monitor init: ${e.message}`); }

  const app = express();
  app.use(express.json({ limit: '10mb' }));

  // ── CORS ──────────────────────────────────────────────────────────────────
  app.use((req, res, next) => {
    res.setHeader('Access-Control-Allow-Origin',  '*');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Mcp-Session-Id');
    res.setHeader('Access-Control-Allow-Methods', 'POST, GET, OPTIONS');
    if (req.method === 'OPTIONS') { res.sendStatus(204); return; }
    next();
  });

  // ── Pre-create a singleton McpServer (reused across all requests) ─────────
  // Creating a fresh McpServer per request was wasteful: it re-registered all
  // 35+ tools on every POST. State lives in SQLite/filesystem — stateless is
  // fine at the data layer, not the server object layer.
  const singletonServer = await serverFactory();
  log.info('McpServer singleton created and ready');

  // ── Tool registry — direct callable map (used by POST /call) ─────────────
  // Populated by mcp-server.mjs via globalThis.__xioToolRegistry.
  // This is the clean escape hatch from SSE: any client can POST /call
  // with { tool, args } and get a plain JSON response synchronously.
  function getRegistry() {
    return globalThis.__xioToolRegistry ?? new Map();
  }
  // ── Streamable HTTP — stateless mode ─────────────────────────────────────
  // Each POST /mcp gets a fresh Transport connected to the singleton McpServer.
  // We close the previous transport before connecting the new one — this avoids
  // "Already connected to a transport" errors from the MCP SDK.
  let _activeTransport = null;

  app.post('/mcp', async (req, res) => {
    log.info(`[/mcp] POST  body_keys=${Object.keys(req.body || {}).join(',')}`);
    try {
      // Close previous transport so the singleton server is free to reconnect
      if (_activeTransport) {
        await _activeTransport.close().catch(() => {});
        _activeTransport = null;
      }

      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: undefined,   // stateless — no session headers
      });
      _activeTransport = transport;

      await singletonServer.connect(transport);
      await transport.handleRequest(req, res, req.body);
    } catch (err) {
      log.error(`[/mcp] ${err.message}`);
      if (!res.headersSent) res.status(500).json({ error: err.message });
    }
  });


  // GET /mcp not supported in stateless mode (no server-push sessions)
  app.get('/mcp', (_, res) => res.status(405).json({
    error: 'Use POST /mcp for stateless Streamable HTTP, or POST /call for direct JSON',
  }));

  // ── POST /call — direct synchronous JSON endpoint ─────────────────────────
  // Replaces the SSE-heavy /mcp for programmatic callers (Python, curl, etc.).
  // Usage:  POST /call  { "tool": "xb_job_poll", "args": { "job_id": "abc123" } }
  // Returns: { "ok": true, "result": { ... } }  or  { "ok": false, "error": "..." }
  // No SSE, no streaming, no connection persistence, no 15s timeouts.
  app.post('/call', async (req, res) => {
    const { tool, args = {} } = req.body ?? {};
    if (!tool) return res.status(400).json({ ok: false, error: 'Missing "tool" field' });
    const registry = getRegistry();
    const entry    = registry.get(tool);
    if (!entry) {
      const available = [...registry.keys()].sort();
      return res.status(404).json({ ok: false, error: `Unknown tool: ${tool}`, available });
    }
    try {
      const result = await entry.handler(args);
      res.json({ ok: true, result });
    } catch (err) {
      log.error(`[/call] ${tool}: ${err.message}`);
      res.status(500).json({ ok: false, tool, error: err.message });
    }
  });

  // GET /call — discovery: list all registered tools
  app.get('/call', (_, res) => {
    const registry = getRegistry();
    const tools = [...registry.keys()].sort().map(name => ({
      name,
      description: registry.get(name)?.description ?? null,
    }));
    res.json({ endpoint: 'POST /call', usage: '{ tool, args }', tools });
  });

  // ── Disconnect endpoint — graceful runtime shutdown ────────────────────────
  // Called by xb_runtime_disconnect for remote node disconnection.
  // Releases all Drive locks then unassigns the Colab runtime.
  app.post('/disconnect', async (req, res) => {
    const { reason = 'remote disconnect' } = req.body ?? {};
    res.json({ ok: true, message: `Disconnecting runtime (reason: ${reason})…` });
    // Async: send response first, then disconnect
    setTimeout(async () => {
      try {
        const { execSync: _exec, spawn: _spawn } = await import('node:child_process');
        // Step 1: release Drive locks
        try { _exec('python3 /content/xio-browser/colab/boot.py --unlock-all',
          { encoding: 'utf8', timeout: 20000 }); } catch (_) {}
        // Step 2: terminate Jupyter kernel (REST API first, pkill fallback)
        // google.colab.runtime.unassign() does NOT work from a subprocess —
        // it requires the Jupyter kernel context. Use the Jupyter REST API instead.
        _spawn('bash', ['-c', `
          for PORT in 9000 8888; do
            KERNELS=$(curl -s --max-time 3 http://localhost:$PORT/api/kernels 2>/dev/null)
            KID=$(echo "$KERNELS" | python3 -c "
import sys, json
try:
    ks = json.load(sys.stdin)
    if ks: print(ks[0]['id'])
except: pass
" 2>/dev/null)
            if [ -n "$KID" ]; then
              curl -s -X DELETE http://localhost:$PORT/api/kernels/$KID 2>/dev/null
              break
            fi
          done
          sleep 1
          pkill -f ipykernel_launcher 2>/dev/null || true
        `], { detached: true, stdio: 'ignore' }).unref();
      } catch (_) { /* best-effort */ }
    }, 500);
  });

  // ── Config endpoint — immediate in-process config override ─────────────────
  // Called by xb_runtime_config(action='set') to apply changes right away
  // without waiting for the 2-min keep-alive Drive refresh cycle.
  // Stores overrides in globalThis.__xioConfigOverrides (checked by keep-alive).
  globalThis.__xioConfigOverrides = globalThis.__xioConfigOverrides ?? {};
  app.post('/config', (req, res) => {
    const patch = req.body ?? {};
    const LIVE = new Set([
      'auto_spawn_enabled', 'auto_spawn_after_minutes', 'auto_spawn_google_signin',
      'lock_stale_minutes', 'job_retention_days', 'default_exit',
    ]);
    const applied = {};
    for (const [k, v] of Object.entries(patch)) {
      if (LIVE.has(k)) {
        globalThis.__xioConfigOverrides[k] = v;
        applied[k] = v;
      }
    }
    log.info(`[/config] Applied overrides: ${JSON.stringify(applied)}`);
    res.json({ ok: true, applied });
  });

  // GET /config — return current in-process overrides
  app.get('/config', (_, res) => {
    res.json({ ok: true, overrides: globalThis.__xioConfigOverrides ?? {} });
  });

  // ── Health ────────────────────────────────────────────────────────────────
  app.get('/health', (_, res) => {
    const job = getRunningJob?.();
    res.json({
      ok:          true,
      service:     'xio-browser',
      version:     '0.5.0',
      port,
      running_job: job ? { job_id: job.jobId, session_id: job.sessionId } : null,
      endpoints: {
        'POST /mcp':  'MCP Streamable HTTP (SSE-framed, for MCP clients)',
        'POST /call': 'Direct JSON tool call — { tool, args } → { ok, result } (no SSE)',
        'GET /call':  'List all registered tools',
        'GET /health': 'This liveness probe',
        'GET /events': 'SSE job lifecycle events (job.started, job.done, etc.)',
        'GET /stream': 'MJPEG live browser view',
        'GET /stream/sse': 'SSE JPEG frames (Safari/EventSource compatible)',
        'GET /stream/sse/view': 'HTML live viewer page',
        'GET /devtools': 'CDP targets + DevTools URLs',
        'GET /jobs/:id': 'Job file listing',
        'GET /jobs/:id/:file': 'Serve job screenshot/result file',
      },
    });
  });

  // ── Job Screenshot Server ─────────────────────────────────────────────────
  // Serves step screenshots as image files instead of inline base64.
  // Agents can embed these URLs in responses rather than huge base64 blobs.
  //
  // Helper: resolve a job dir from either short ID or full datetime-prefixed dirName.
  function resolveJobHttpDir(jobId) {
    let jobDir = Paths.jobDir(jobId);
    if (!fs.existsSync(jobDir)) {
      const jobsBase = Paths.jobs();
      if (fs.existsSync(jobsBase)) {
        const match = fs.readdirSync(jobsBase).find(d => d.endsWith(jobId));
        if (match) jobDir = path.join(jobsBase, match);
      }
    }
    return jobDir;
  }

  // Serve a file one level deep in a subdir (e.g. steps/oauth_screen_A.png)
  app.get('/jobs/:jobId/:subdir/:filename', (req, res) => {
    const { jobId, subdir, filename } = req.params;
    if (!/^[\w-]+$/.test(jobId))   return res.status(400).json({ error: 'Invalid job ID' });
    if (!/^[\w-]+$/.test(subdir))  return res.status(400).json({ error: 'Invalid subdir' });
    if (!filename.match(/^[\w.-]+\.(png|jpg|jpeg|webp|json)$/))
      return res.status(400).json({ error: 'Invalid filename' });
    const jobDir = resolveJobHttpDir(jobId);
    if (!fs.existsSync(jobDir)) return res.status(404).json({ error: 'Job not found' });
    const p = path.join(jobDir, subdir, filename);
    if (!fs.existsSync(p)) return res.status(404).json({ error: 'File not found' });
    res.sendFile(p);
  });

  // Serve a flat job file (e.g. 00_verify_session.jpg, result.json)
  app.get('/jobs/:jobId/:filename', (req, res) => {
    const { jobId, filename } = req.params;
    if (!/^[\w-]+$/.test(jobId)) return res.status(400).json({ error: 'Invalid job ID' });
    if (!filename.match(/^[\w.-]+\.(png|jpg|jpeg|webp|json)$/))
      return res.status(400).json({ error: 'Invalid filename' });
    const jobDir = resolveJobHttpDir(jobId);
    if (!fs.existsSync(jobDir)) return res.status(404).json({ error: 'Job not found' });
    const p = path.join(jobDir, filename);
    if (!fs.existsSync(p)) return res.status(404).json({ error: 'File not found' });
    res.sendFile(p);
  });

  // List all files in a job dir (flat files + all subdirs including steps/ and nested sub-workflow dirs)
  app.get('/jobs/:jobId', (req, res) => {
    const { jobId } = req.params;
    if (!/^[\w-]+$/.test(jobId)) return res.status(400).json({ error: 'Invalid job ID' });
    const jobDir = resolveJobHttpDir(jobId);
    if (!fs.existsSync(jobDir)) return res.status(404).json({ error: 'Job not found' });
    const selfUrl = `http://${selfIP() || '127.0.0.1'}:${port}`;
    const files = [];
    // Walk top level + all subdirs (steps/, nested JOB_* sub-workflow dirs, etc.)
    for (const f of fs.readdirSync(jobDir)) {
      const fp = path.join(jobDir, f);
      const st = fs.statSync(fp);
      if (st.isDirectory()) {
        for (const sf of fs.readdirSync(fp)) {
          files.push({
            name: `${f}/${sf}`,
            url:  `${selfUrl}/jobs/${jobId}/${f}/${sf}`,
            size: fs.statSync(path.join(fp, sf)).size,
          });
        }
      } else {
        files.push({ name: f, url: `${selfUrl}/jobs/${jobId}/${f}`, size: st.size });
      }
    }
    res.json({ job_id: jobId, dir: jobDir, files });
  });

  // ── SSE Job Lifecycle Events ──────────────────────────────────────────────
  // Agent subscribes to GET /events and receives push notifications:
  //   job.started, job.step.done, job.step.error, job.done, job.error
  // This eliminates polling — agent waits for events instead.
  const _sseClients = new Set();
  function emitEvent(type, data) {
    const payload = `data: ${JSON.stringify({ type, ...data, ts: Date.now() })}\n\n`;
    for (const res of _sseClients) {
      try { res.write(payload); } catch { _sseClients.delete(res); }
    }
  }
  // Expose emitEvent globally so job-manager can call it
  globalThis.__xioEmitEvent = emitEvent;

  app.get('/events', (req, res) => {
    res.writeHead(200, {
      'Content-Type':                'text/event-stream',
      'Cache-Control':               'no-cache',
      'Connection':                  'keep-alive',
      'Access-Control-Allow-Origin': '*',
    });
    res.setTimeout(0);
    res.write(': XIO-Browser SSE connected\n\n');
    _sseClients.add(res);
    log.info(`SSE client connected — total: ${_sseClients.size}`);
    // Keep-alive
    const ka = setInterval(() => { try { res.write(': ka\n\n'); } catch { clearInterval(ka); } }, 15000);
    req.on('close', () => { _sseClients.delete(res); clearInterval(ka); });
  });

  // ── MJPEG Live Stream ─────────────────────────────────────────────────────
  // multipart/x-mixed-replace — works in Chrome, Firefox, curl, VLC.
  // ⚠️  Safari on Mac does NOT support MJPEG natively → use /stream/sse instead.
  app.get('/stream', (req, res) => {
    log.info(`[/stream] MJPEG client ${req.ip} connected`);
    registerClient(res);
  });

  // ── SSE Frame Stream (/stream/sse) — Mac / Safari / EventSource compatible ─
  // Delivers each JPEG frame as a base64 data-URI inside an SSE event.
  // The X-Accel-Buffering: no header prevents Tailscale / nginx from batching
  // the stream, which is the #1 cause of "nothing showing" on Mac.
  //
  //   Quick test (Mac terminal):  curl http://100.64.36.15:4242/stream/sse
  //   Browser viewer:             http://100.64.36.15:4242/stream/sse/view
  app.get('/stream/sse', async (req, res) => {
    log.info(`[/stream/sse] SSE client ${req.ip} connected`);
    res.writeHead(200, {
      'Content-Type':                'text/event-stream',
      'Cache-Control':               'no-cache, no-store',
      'Connection':                  'keep-alive',
      'X-Accel-Buffering':           'no',
      'Access-Control-Allow-Origin': '*',
    });
    res.setTimeout(0);
    res.write(': XIO-Browser SSE stream connected\n\n');

    const { subscribeFrames, unsubscribeFrames } = await import('./core/screencaster.mjs');
    const send = (jpegBuf) => {
      const b64     = jpegBuf.toString('base64');
      const payload = JSON.stringify({ frame: `data:image/jpeg;base64,${b64}`, ts: Date.now() });
      try { res.write(`data: ${payload}\n\n`); } catch { /* client dropped */ }
    };

    const subId = subscribeFrames(send);
    const ka    = setInterval(() => { try { res.write(': ka\n\n'); } catch { clearInterval(ka); } }, 15_000);
    req.on('close', () => { unsubscribeFrames(subId); clearInterval(ka); });
  });

  // ── SSE Live Viewer HTML page ─────────────────────────────────────────────
  // Open http://100.64.36.15:4242/stream/sse/view in any Mac browser.
  // Uses EventSource API — works in Safari, Chrome, Firefox.
  app.get('/stream/sse/view', (req, res) => {
    // Note: tsIP no longer needed in JS — we use window.location.origin instead
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(`<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>XIO Browser — Live View</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d0d0d; color: #e0e0e0; font-family: monospace;
         display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
  header { width: 100%; background: #111; padding: 10px 20px;
           display: flex; align-items: center; gap: 12px; border-bottom: 1px solid #222; }
  header h1 { font-size: 14px; color: #00e676; letter-spacing: 1px; }
  #dot { width: 8px; height: 8px; border-radius: 50%; background: #555; }
  #dot.live { background: #00e676; box-shadow: 0 0 6px #00e676; animation: pulse 1.2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  #status { font-size: 11px; color: #888; }
  #fps    { font-size: 11px; color: #888; margin-left: auto; }
  #frame-wrap { flex: 1; display: flex; align-items: center; justify-content: center;
                padding: 16px; width: 100%; }
  #frame { max-width: 100%; max-height: calc(100vh - 80px);
           border: 1px solid #222; border-radius: 4px; background: #111; }
</style></head>
<body>
<header>
  <div id="dot"></div>
  <h1>XIO BROWSER · LIVE VIEW</h1>
  <span id="status">Connecting…</span>
  <span id="fps"></span>
</header>
<div id="frame-wrap">
  <img id="frame" alt="Live browser view" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=">
</div>
<script>
const img    = document.getElementById('frame');
const dot    = document.getElementById('dot');
const status = document.getElementById('status');
const fpsEl  = document.getElementById('fps');
let frames = 0, lastTs = Date.now();

// Use window.location so this works over both http:// and https:// (Tailscale HTTPS proxy)
const sseUrl = window.location.origin + '/stream/sse';
const es = new EventSource(sseUrl);

es.onopen = () => {
  dot.className = 'live';
  status.textContent = 'Connected — waiting for first frame…';
};
es.onmessage = e => {
  try {
    const d = JSON.parse(e.data);
    if (d.frame) {
      img.src = d.frame;
      frames++;
      const now = Date.now();
      const fps = (1000 / (now - lastTs)).toFixed(1);
      lastTs = now;
      status.textContent = 'Live ✅';
      fpsEl.textContent  = fps + ' fps · ' + frames + ' frames';
    }
  } catch {}
};
es.onerror = () => {
  dot.className = '';
  // Under an HTTPS Tailscale proxy, EventSource is blocked because the
  // proxy does SSE-incompatible TLS termination. Surface the direct URL.
  const directUrl = 'http://' + window.location.hostname + ':' + (window.location.port || '4242') + '/stream/sse/view';
  status.textContent = window.location.protocol === 'https:'
    ? 'SSE blocked by HTTPS proxy. Open directly: ' + directUrl
    : 'Reconnecting…';
};
</script></body></html>`);
  });

  // ── Multi-Instance Stream Routes ───────────────────────────────────────────

  // List all registered streams
  app.get('/stream/list', (req, res) => {
    const tsIP = selfIP() || '127.0.0.1';
    const streams = listStreams().map(s => ({
      ...s,
      stream_url:     `http://${tsIP}:${port}/stream/${s.id}`,
      stream_sse_url: `http://${tsIP}:${port}/stream/${s.id}/sse`,
      view_url:       `http://${tsIP}:${port}/stream/${s.id}/view`,
    }));
    const resource = getResourceSnapshot();
    res.json({ streams, resource, grid_url: `http://${tsIP}:${port}/stream/grid` });
  });

  // Resource snapshot endpoint
  app.get('/stream/resources', (req, res) => {
    res.json(getResourceSnapshot());
  });

  // Per-instance MJPEG stream
  app.get('/stream/:id([a-zA-Z0-9_-]+)', (req, res) => {
    const { id } = req.params;
    // Backward compat: /stream without ID is handled by existing route above (xio-main)
    const stream = getStream(id);
    if (!stream) { res.status(404).json({ error: `Stream '${id}' not found` }); return; }
    log.info(`[/stream/${id}] MJPEG client ${req.ip} connected`);
    registerMjpegClient(id, res);
  });

  // Per-instance SSE stream
  app.get('/stream/:id([a-zA-Z0-9_-]+)/sse', (req, res) => {
    const { id } = req.params;
    const stream = getStream(id);
    if (!stream) { res.status(404).json({ error: `Stream '${id}' not found` }); return; }
    log.info(`[/stream/${id}/sse] SSE client ${req.ip} connected`);
    registerSseClient(id, res);
  });

  // Per-instance single viewer
  app.get('/stream/:id([a-zA-Z0-9_-]+)/view', (req, res) => {
    const { id } = req.params;
    const stream = getStream(id);
    if (!stream) { res.status(404).json({ error: `Stream '${id}' not found` }); return; }
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(_singleViewerHtml(id, stream.label));
  });

  // Push frame from external source (broker CDP screencast) — localhost only
  app.post('/stream/:id([a-zA-Z0-9_-]+)/push-frame', express.raw({ type: 'image/jpeg', limit: '2mb' }), (req, res) => {
    const { id } = req.params;
    // Security: only accept from localhost
    const ip = req.ip?.replace('::ffff:', '') ?? '';
    if (ip !== '127.0.0.1' && ip !== '::1' && ip !== 'localhost') {
      res.status(403).json({ error: 'push-frame only from localhost' });
      return;
    }
    pushFrame(id, req.body);
    res.json({ ok: true });
  });

  // Register/unregister streams via HTTP (for broker integration)
  app.post('/stream/register', (req, res) => {
    const { id, label, type } = req.body;
    if (!id || !label) { res.status(400).json({ error: 'id and label required' }); return; }
    // Register with a push-only getFrame (frames come via push-frame endpoint)
    registerStream(id, label, type ?? 'appetize', async () => null, 2);
    log.info(`[stream] Registered external stream: ${id} (${label})`);
    res.json({ ok: true, stream_id: id });
  });

  app.post('/stream/unregister', (req, res) => {
    const { id } = req.body;
    if (!id) { res.status(400).json({ error: 'id required' }); return; }
    unregisterStream(id);
    log.info(`[stream] Unregistered external stream: ${id}`);
    res.json({ ok: true });
  });

  // ── Grid Viewer (all instances) ───────────────────────────────────────────
  app.get('/stream/grid', (req, res) => {
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(_gridViewerHtml());
  });

  // ── DevTools Discovery ────────────────────────────────────────────────────
  // Returns everything the Mac needs to auto-connect:
  //   • stream_url       — MJPEG live view
  //   • inspector_url    — Chrome DevTools inspector for the active page
  //   • devtools_http    — Chrome debug endpoint (direct Tailscale access)
  //   • targets[]        — CDP page targets with Tailscale-rewritten WS URLs
  app.get('/devtools', async (req, res) => {
    try {
      const tsIP = selfIP() || '127.0.0.1';
      const streamInfo = getStreamInfo(port, tsIP);

      // Fetch Chrome's native target list from port 9222
      let targets = [];
      try {
        const r = await fetch('http://127.0.0.1:9222/json', { signal: AbortSignal.timeout(2000) });
        const raw = await r.json();
        // Replace 127.0.0.1 with Tailscale IP so Mac can connect directly
        targets = raw
          .filter(t => t.type === 'page' && !t.url.startsWith('devtools://'))
          .map(t => ({
            id:    t.id,
            url:   t.url,
            title: t.title,
            type:  t.type,
            webSocketDebuggerUrl: t.webSocketDebuggerUrl?.replace('127.0.0.1', tsIP),
            // Full inspector URL — open this in Chrome on Mac for DevTools UI
            inspector_url: t.webSocketDebuggerUrl
              ? `devtools://devtools/bundled/inspector.html?ws=${t.webSocketDebuggerUrl.replace('ws://', '').replace('127.0.0.1', tsIP)}`
              : null,
          }));
      } catch {
        // Chrome may not be launched yet — return empty targets, stream still works
      }

      res.json({
        ...streamInfo,
        colab_ip:       tsIP,
        devtools_http:  `http://${tsIP}:9222`,
        devtools_json:  `http://${tsIP}:9222/json`,
        targets,
        // Convenience: top-level inspector URL for the first page
        inspector_url:  targets[0]?.inspector_url ?? null,
        // Quickstart commands for Mac
        quickstart: {
          view_stream:    `open "${streamInfo.stream_url}"`,
          open_inspector: targets[0]?.inspector_url
            ? `open "${targets[0].inspector_url}"`
            : `open "http://${tsIP}:9222"`,
          ssh_tunnel:     `ssh -f -N -L 9222:localhost:9222 root@${tsIP}`,
        },
      });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  app.listen(port, '0.0.0.0', () => {
    log.info(`HTTP server listening on 0.0.0.0:${port}`);
    log.info(`  POST /mcp  — MCP Streamable HTTP (SSE)`);
    log.info(`  POST /call — Direct JSON tool call (no SSE)`);
    log.info(`  GET  /call — List all tools`);
    log.info(`  GET  /health, /stream, /devtools, /events`);
    log.info(`  GET  /stream/grid — Multi-instance grid viewer`);
    log.info(`  GET  /stream/list — JSON stream registry`);
  });
}

// ── Grid Viewer HTML ────────────────────────────────────────────────────────
function _gridViewerHtml() {
  return `<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>XIO Mesh · Stream Grid</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0a0a0a; color: #e0e0e0; font-family: 'SF Mono', monospace; }
  header { background: #111; padding: 10px 20px; display: flex; align-items: center; gap: 12px;
           border-bottom: 1px solid #222; position: sticky; top: 0; z-index: 10; }
  header h1 { font-size: 14px; color: #00e676; letter-spacing: 1px; }
  .mode-btn { background: #222; color: #aaa; border: 1px solid #333; padding: 4px 10px;
              border-radius: 4px; cursor: pointer; font-size: 11px; }
  .mode-btn.active { background: #00e676; color: #000; border-color: #00e676; }
  #resource-bar { margin-left: auto; font-size: 11px; color: #888; display: flex; gap: 12px; }
  #grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
          gap: 8px; padding: 12px; }
  .cell { background: #111; border: 1px solid #222; border-radius: 6px; overflow: hidden;
          cursor: pointer; position: relative; transition: all 0.2s; }
  .cell:hover { border-color: #00e676; }
  .cell.focused { grid-column: 1 / -1; grid-row: 1; }
  .cell-header { display: flex; align-items: center; gap: 8px; padding: 6px 10px;
                 background: #1a1a1a; font-size: 11px; }
  .badge { padding: 2px 6px; border-radius: 3px; font-size: 10px; font-weight: bold; }
  .badge.live { background: #00e676; color: #000; }
  .badge.idle { background: #ffd740; color: #000; }
  .badge.paused { background: #ff5252; color: #fff; }
  .cell-label { flex: 1; color: #ccc; }
  .cell-fps { color: #888; }
  .cell img { width: 100%; display: block; background: #000; min-height: 200px; }
  #pip-strip { display: none; position: fixed; bottom: 0; left: 0; right: 0;
               background: #111; border-top: 1px solid #333; padding: 6px 12px;
               gap: 8px; z-index: 20; overflow-x: auto; white-space: nowrap; }
  #pip-strip.visible { display: flex; }
  .pip-thumb { width: 120px; height: 80px; object-fit: cover; border: 1px solid #333;
               border-radius: 4px; cursor: pointer; flex-shrink: 0; }
  .pip-thumb:hover { border-color: #00e676; }
  .no-streams { text-align: center; padding: 60px; color: #555; font-size: 14px; }
</style></head>
<body>
<header>
  <h1>XIO MESH · STREAMS</h1>
  <button class="mode-btn active" id="btn-grid" onclick="setMode('grid')">GRID</button>
  <button class="mode-btn" id="btn-focus" onclick="setMode('focus')">FOCUS</button>
  <div id="resource-bar">
    <span id="ram-info">RAM: —</span>
    <span id="fps-info">FPS: —</span>
    <span id="level-info">Level: —</span>
  </div>
</header>
<div id="grid"></div>
<div id="pip-strip"></div>
<script>
const grid = document.getElementById('grid');
const pipStrip = document.getElementById('pip-strip');
let streams = {};  // id → { es, img, data }
let mode = 'grid';
let focusedId = null;

async function refresh() {
  try {
    const r = await fetch('/stream/list');
    const d = await r.json();
    // Update resource bar
    if (d.resource) {
      document.getElementById('ram-info').textContent = 'RAM: ' + (d.resource.freeGB?.toFixed(1) ?? '?') + ' GB';
      document.getElementById('fps-info').textContent = 'FPS: ' + (d.resource.fps ?? '?');
      document.getElementById('level-info').textContent = 'Level: ' + (d.resource.level ?? '?');
    }
    const ids = new Set(d.streams.map(s => s.id));
    // Remove dead streams
    for (const [id, s] of Object.entries(streams)) {
      if (!ids.has(id)) { s.es?.close(); delete streams[id]; }
    }
    // Add new streams
    for (const s of d.streams) {
      if (!streams[s.id]) addStream(s);
      else streams[s.id].data = s;  // update metadata
    }
    render();
  } catch {}
}

function addStream(s) {
  const es = new EventSource('/stream/' + s.id + '/sse');
  const entry = { es, img: null, data: s, lastTs: 0, fpsCalc: 0, frames: 0 };
  es.onmessage = e => {
    try {
      const d = JSON.parse(e.data);
      if (d.frame && entry.img) {
        entry.img.src = d.frame;
        entry.frames++;
        const now = Date.now();
        entry.fpsCalc = (1000 / Math.max(1, now - entry.lastTs)).toFixed(1);
        entry.lastTs = now;
        // Update FPS badge
        const badge = document.querySelector('#fps-' + s.id);
        if (badge) badge.textContent = entry.fpsCalc + ' fps';
        // Update status badge
        const sBadge = document.querySelector('#status-' + s.id);
        if (sBadge) { sBadge.className = 'badge live'; sBadge.textContent = 'LIVE'; }
      }
    } catch {}
  };
  es.onerror = () => {
    const sBadge = document.querySelector('#status-' + s.id);
    if (sBadge) { sBadge.className = 'badge idle'; sBadge.textContent = 'IDLE'; }
  };
  streams[s.id] = entry;
}

function render() {
  const ids = Object.keys(streams);
  if (ids.length === 0) {
    grid.innerHTML = '<div class="no-streams">No active streams. Start a job or broker session to see live feeds.</div>';
    pipStrip.className = '';
    return;
  }
  grid.innerHTML = '';
  pipStrip.innerHTML = '';
  for (const id of ids) {
    const s = streams[id];
    const cell = document.createElement('div');
    cell.className = 'cell' + (mode === 'focus' && focusedId === id ? ' focused' : '');
    if (mode === 'focus' && focusedId && focusedId !== id) { cell.style.display = 'none'; }
    cell.innerHTML = '<div class="cell-header">' +
      '<span class="badge live" id="status-' + id + '">LIVE</span>' +
      '<span class="cell-label">' + (s.data.label || id) + '</span>' +
      '<span class="cell-fps" id="fps-' + id + '">' + (s.fpsCalc || '0') + ' fps</span>' +
      '</div>' +
      '<img alt="' + id + '" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=">';
    cell.querySelector('img').onclick = () => { focusedId = id; setMode('focus'); };
    grid.appendChild(cell);
    s.img = cell.querySelector('img');
    // PiP thumbnails
    if (mode === 'focus' && focusedId !== id) {
      const thumb = document.createElement('img');
      thumb.className = 'pip-thumb';
      thumb.alt = id;
      thumb.onclick = () => { focusedId = id; render(); };
      s.pipImg = thumb;
      pipStrip.appendChild(thumb);
    }
  }
  pipStrip.className = (mode === 'focus' && ids.length > 1) ? 'visible' : '';
}

function setMode(m) {
  mode = m;
  document.getElementById('btn-grid').className = 'mode-btn' + (m === 'grid' ? ' active' : '');
  document.getElementById('btn-focus').className = 'mode-btn' + (m === 'focus' ? ' active' : '');
  if (m === 'grid') focusedId = null;
  if (m === 'focus' && !focusedId) focusedId = Object.keys(streams)[0] ?? null;
  render();
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') setMode('grid');
  const ids = Object.keys(streams);
  if (mode === 'focus' && ids.length > 1) {
    const idx = ids.indexOf(focusedId);
    if (e.key === 'ArrowRight') { focusedId = ids[(idx + 1) % ids.length]; render(); }
    if (e.key === 'ArrowLeft') { focusedId = ids[(idx - 1 + ids.length) % ids.length]; render(); }
  }
});

refresh();
setInterval(refresh, 5000);
</script></body></html>`;
}

// ── Single Stream Viewer HTML ──────────────────────────────────────────────
function _singleViewerHtml(streamId, label) {
  return `<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>${label || streamId} — XIO Stream</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d0d0d; color: #e0e0e0; font-family: monospace;
         display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
  header { width: 100%; background: #111; padding: 10px 20px;
           display: flex; align-items: center; gap: 12px; border-bottom: 1px solid #222; }
  header h1 { font-size: 14px; color: #00e676; letter-spacing: 1px; }
  #dot { width: 8px; height: 8px; border-radius: 50%; background: #555; }
  #dot.live { background: #00e676; box-shadow: 0 0 6px #00e676; animation: pulse 1.2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  #status { font-size: 11px; color: #888; }
  #fps    { font-size: 11px; color: #888; margin-left: auto; }
  #frame-wrap { flex: 1; display: flex; align-items: center; justify-content: center;
                padding: 16px; width: 100%; }
  #frame { max-width: 100%; max-height: calc(100vh - 80px);
           border: 1px solid #222; border-radius: 4px; background: #111; }
</style></head>
<body>
<header>
  <div id="dot"></div>
  <h1>${(label || streamId).toUpperCase()} · LIVE VIEW</h1>
  <span id="status">Connecting…</span>
  <span id="fps"></span>
</header>
<div id="frame-wrap">
  <img id="frame" alt="Live view" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=">
</div>
<script>
const img = document.getElementById('frame');
const dot = document.getElementById('dot');
const status = document.getElementById('status');
const fpsEl = document.getElementById('fps');
let frames = 0, lastTs = Date.now();
const es = new EventSource(window.location.origin + '/stream/${streamId}/sse');
es.onopen = () => { dot.className = 'live'; status.textContent = 'Connected — waiting for first frame…'; };
es.onmessage = e => {
  try {
    const d = JSON.parse(e.data);
    if (d.frame) {
      img.src = d.frame;
      frames++;
      const now = Date.now();
      const fps = (1000 / (now - lastTs)).toFixed(1);
      lastTs = now;
      status.textContent = 'Live ✅';
      fpsEl.textContent = fps + ' fps · ' + frames + ' frames';
    }
  } catch {}
};
es.onerror = () => { dot.className = ''; status.textContent = 'Reconnecting…'; };
</script></body></html>`;
}
