/**
 * Stream Registry
 * Central registry for ALL active browser streams in the XIO Mesh.
 * Handles MJPEG and SSE broadcasting, dynamic FPS control, and client lifecycle.
 */
import { createLogger } from '../utils/logger.mjs';

const log = createLogger('stream-registry');

// ── Constants ──
const BOUNDARY = 'XIOFRAME';
const KEEP_ALIVE_INTERVAL = 15000;
const DEFAULT_GLOBAL_FPS = 4;

// ── State ──
const streams = new Map();
let _globalFps = DEFAULT_GLOBAL_FPS;
let _primaryStreamId = null;
let keepAliveTimer = null;

// ── Internal Helpers ──
function getEffectiveFps(stream) {
  return Math.min(stream._baseFps, _globalFps);
}

function sendMjpegFrame(res, buffer) {
  const header = `--${BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: ${buffer.length}\r\n\r\n`;
  const footer = `\r\n`;
  try {
    res.write(header);
    res.write(buffer);
    res.write(footer);
  } catch (err) {
    // Error handled in broadcast loop or client disconnect
    throw err;
  }
}

function sendSseFrame(res, buffer, streamId) {
  const base64 = buffer.toString('base64');
  const data = JSON.stringify({
    frame: `data:image/jpeg;base64,${base64}`,
    ts: Date.now(),
    stream_id: streamId
  });
  try {
    res.write(`data: ${data}\n\n`);
  } catch (err) {
    throw err;
  }
}

function broadcastFrame(stream, buffer) {
  stream._lastFrame = buffer;
  
  for (const res of stream.clients) {
    try {
      sendMjpegFrame(res, buffer);
    } catch (err) {
      log.error(`Error sending MJPEG frame to client: ${err.message}`);
      stream.clients.delete(res);
      checkCaptureState(stream);
    }
  }
  
  for (const res of stream.sseClients) {
    try {
      sendSseFrame(res, buffer, stream.id);
    } catch (err) {
      log.error(`Error sending SSE frame to client: ${err.message}`);
      stream.sseClients.delete(res);
      checkCaptureState(stream);
    }
  }
}

function checkCaptureState(stream) {
  const hasClients = stream.clients.size > 0 || stream.sseClients.size > 0;
  if (!hasClients && stream._timer) {
    clearTimeout(stream._timer);
    stream._timer = null;
  } else if (hasClients && !stream._timer) {
    scheduleCapture(stream);
  }
}

function scheduleCapture(stream) {
  if (stream._timer) {
    clearTimeout(stream._timer);
    stream._timer = null;
  }
  
  const effectiveFps = getEffectiveFps(stream);
  const hasClients = stream.clients.size > 0 || stream.sseClients.size > 0;
  
  if (effectiveFps <= 0 || !hasClients) {
    return;
  }
  
  const interval = Math.floor(1000 / effectiveFps);
  stream._timer = setTimeout(async () => {
    try {
      if (stream.getFrame) {
        const frame = await stream.getFrame();
        if (frame) {
          broadcastFrame(stream, frame);
        }
      }
    } catch (err) {
      log.error(`Error capturing frame for stream ${stream.id}: ${err.message}`);
    } finally {
      if (streams.has(stream.id)) {
        scheduleCapture(stream);
      }
    }
  }, interval);
}

function startKeepAlive() {
  if (keepAliveTimer) return;
  keepAliveTimer = setInterval(() => {
    for (const stream of streams.values()) {
      const mjpegKa = `--${BOUNDARY}\r\nContent-Type: text/plain\r\n\r\nka\r\n`;
      for (const res of stream.clients) {
        try { res.write(mjpegKa); } catch (err) { stream.clients.delete(res); }
      }
      
      const sseKa = `: ka\n\n`;
      for (const res of stream.sseClients) {
        try { res.write(sseKa); } catch (err) { stream.sseClients.delete(res); }
      }
    }
  }, KEEP_ALIVE_INTERVAL);
}

function stopKeepAlive() {
  if (keepAliveTimer) {
    clearInterval(keepAliveTimer);
    keepAliveTimer = null;
  }
}

// ── Exported Functions ──

export function registerStream(id, label, type, getFrame, baseFps = 4) {
  if (streams.has(id)) {
    log.warn(`Stream ${id} already registered. Overwriting.`);
    unregisterStream(id);
  }
  
  const stream = {
    id,
    label,
    type,
    jobId: null,
    getFrame,
    fps: Math.min(baseFps, _globalFps),
    clients: new Set(),
    sseClients: new Set(),
    _lastFrame: null,
    _timer: null,
    _baseFps: baseFps
  };
  
  streams.set(id, stream);
  log.info(`Registered stream ${id} (${label}, ${type}), baseFps: ${baseFps}`);
  
  if (streams.size === 1) startKeepAlive();
  
  // Capture will start automatically when a client connects via checkCaptureState
  scheduleCapture(stream);
  
  return stream;
}

export function unregisterStream(id) {
  const stream = streams.get(id);
  if (!stream) return;
  
  if (stream._timer) {
    clearTimeout(stream._timer);
  }
  
  for (const res of stream.clients) res.end();
  for (const res of stream.sseClients) res.end();
  
  streams.delete(id);
  log.info(`Unregistered stream ${id}`);
  
  if (id === _primaryStreamId) {
    _primaryStreamId = null;
  }
  
  if (streams.size === 0) stopKeepAlive();
}

export function listStreams() {
  const list = [];
  for (const stream of streams.values()) {
    list.push({
      id: stream.id,
      label: stream.label,
      type: stream.type,
      fps: getEffectiveFps(stream),
      clientCount: stream.clients.size,
      sseClientCount: stream.sseClients.size,
      active: stream._timer !== null
    });
  }
  return list;
}

export function getStream(id) {
  return streams.get(id) || null;
}

export function setGlobalFps(fps) {
  _globalFps = fps;
  log.info(`Set global FPS to ${_globalFps}`);
  
  for (const stream of streams.values()) {
    stream.fps = getEffectiveFps(stream);
    scheduleCapture(stream);
  }
}

export function pauseAllStreams() {
  setGlobalFps(0);
}

export function resumeAllStreams() {
  // Removing the global throttle by setting it to a sufficiently high value
  // so each stream's baseFps becomes the effective limit.
  setGlobalFps(Number.MAX_SAFE_INTEGER);
}

export function setPrimaryStream(id) {
  if (streams.has(id)) {
    _primaryStreamId = id;
    log.info(`Primary stream set to ${id}`);
  }
}

export function getPrimaryStream() {
  if (_primaryStreamId && streams.has(_primaryStreamId)) {
    return streams.get(_primaryStreamId);
  }
  // Fallback to first registered stream
  const first = streams.values().next().value;
  return first || null;
}

export function registerMjpegClient(streamId, res) {
  const stream = streams.get(streamId);
  if (!stream) {
    res.writeHead(404);
    res.end('Stream not found');
    return;
  }
  
  res.writeHead(200, {
    'Content-Type': `multipart/x-mixed-replace; boundary=${BOUNDARY}`,
    'Cache-Control': 'no-cache, no-store, must-revalidate',
    'Connection': 'keep-alive',
    'Pragma': 'no-cache'
  });
  
  stream.clients.add(res);
  log.info(`MJPEG client connected to ${streamId}. Total: ${stream.clients.size}`);
  
  if (stream._lastFrame) {
    try {
      sendMjpegFrame(res, stream._lastFrame);
    } catch (err) {
      log.error(`Error sending initial MJPEG frame: ${err.message}`);
    }
  }
  
  res.on('close', () => {
    stream.clients.delete(res);
    log.info(`MJPEG client disconnected from ${streamId}. Remaining: ${stream.clients.size}`);
    checkCaptureState(stream);
  });
  
  checkCaptureState(stream);
}

export function registerSseClient(streamId, res) {
  const stream = streams.get(streamId);
  if (!stream) {
    res.writeHead(404);
    res.end('Stream not found');
    return;
  }
  
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache, no-store, must-revalidate',
    'Connection': 'keep-alive'
  });
  
  stream.sseClients.add(res);
  log.info(`SSE client connected to ${streamId}. Total: ${stream.sseClients.size}`);
  
  if (stream._lastFrame) {
    try {
      sendSseFrame(res, stream._lastFrame, stream.id);
    } catch (err) {
      log.error(`Error sending initial SSE frame: ${err.message}`);
    }
  }
  
  res.on('close', () => {
    stream.sseClients.delete(res);
    log.info(`SSE client disconnected from ${streamId}. Remaining: ${stream.sseClients.size}`);
    checkCaptureState(stream);
  });
  
  checkCaptureState(stream);
}

export function pushFrame(streamId, jpegBuffer) {
  const stream = streams.get(streamId);
  if (stream) {
    broadcastFrame(stream, jpegBuffer);
  }
}
