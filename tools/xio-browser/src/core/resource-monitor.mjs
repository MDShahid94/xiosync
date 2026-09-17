/**
 * src/core/resource-monitor.mjs
 * 
 * Monitors RAM and CPU usage on the Colab runtime and auto-degrades stream FPS 
 * when resources are constrained.
 */

import { readFileSync } from 'node:fs';
import os from 'node:os';
import { createLogger } from '../utils/logger.mjs';

// ── Configuration ──

const log = createLogger('resource-mon');

const LEVELS = [
  { minFreeGB: 3.0, fps: 4, label: 'normal' },
  { minFreeGB: 2.0, fps: 2, label: 'moderate' },
  { minFreeGB: 1.2, fps: 1, label: 'low' },
  { minFreeGB: 0.7, fps: 0, label: 'critical' },  // streaming fully paused
];

const POLL_INTERVAL_MS = 5000;
const CPU_GUARD_THRESHOLD = 0.85;

// ── State ──

let intervalId = null;
let cpuCount = 1;
let currentLevel = null;
let currentFps = -1;
let setGlobalFpsCallback = null;

let _snapshot = {
  freeGB: 0,
  loadAvg: 0,
  cpuCount: 1,
  fps: 0,
  level: 'unknown',
  lastSampleAt: 0
};

// ── Helpers ──

function getMemAvailableGB() {
  try {
    const meminfo = readFileSync('/proc/meminfo', 'utf8');
    const match = meminfo.match(/MemAvailable:\s+(\d+)\s+kB/);
    if (match && match[1]) {
      return parseInt(match[1], 10) / (1024 * 1024);
    }
  } catch (error) {
    // Graceful fallback if /proc/meminfo doesn't exist (e.g. Mac dev)
    return 8.0;
  }
  return 8.0;
}

function getLoadAvg1m() {
  try {
    const loadavg = readFileSync('/proc/loadavg', 'utf8');
    const parts = loadavg.split(/\s+/);
    if (parts.length > 0) {
      return parseFloat(parts[0]);
    }
  } catch (error) {
    // Graceful fallback if /proc/loadavg doesn't exist
    return 0.5;
  }
  return 0.5;
}

function pollResources() {
  const freeGB = getMemAvailableGB();
  const loadAvg = getLoadAvg1m();
  
  let targetFps = 0;
  let targetLevel = 'critical';

  // Walk LEVELS top to bottom; first entry where freeGB >= minFreeGB wins
  for (const level of LEVELS) {
    if (freeGB >= level.minFreeGB) {
      targetFps = level.fps;
      targetLevel = level.label;
      break;
    }
  }

  // CPU guard: if loadAvg1m / cpuCount > 0.85, halve the computed FPS (floor)
  if (loadAvg / cpuCount > CPU_GUARD_THRESHOLD) {
    targetFps = Math.floor(targetFps / 2);
  }

  const now = Date.now();
  _snapshot = {
    freeGB,
    loadAvg,
    cpuCount,
    fps: targetFps,
    level: targetLevel,
    lastSampleAt: now
  };

  // Log level transitions and notify callback if FPS changes
  if (targetFps !== currentFps || targetLevel !== currentLevel) {
    if (currentLevel !== null) {
      log.info(`Resource transition: ${currentLevel} -> ${targetLevel} (FPS: ${currentFps} -> ${targetFps}) | Free RAM: ${freeGB.toFixed(2)}GB, Load: ${loadAvg.toFixed(2)}`);
    } else {
      log.info(`Initial resource level: ${targetLevel} (FPS: ${targetFps})`);
    }
    
    currentFps = targetFps;
    currentLevel = targetLevel;

    if (typeof setGlobalFpsCallback === 'function') {
      try {
        setGlobalFpsCallback(targetFps);
      } catch (error) {
        log.error(`Error calling setGlobalFps: ${error.message}`);
      }
    }
  }
}

// ── Exports ──

export function startMonitor(setGlobalFpsFn) {
  if (intervalId !== null) {
    log.info('Monitor is already running.');
    return;
  }
  
  try {
    cpuCount = os.cpus().length || 1;
  } catch (error) {
    cpuCount = 1;
  }
  
  setGlobalFpsCallback = setGlobalFpsFn;
  
  log.info(`Starting resource monitor (poll every ${POLL_INTERVAL_MS}ms, CPU Count: ${cpuCount})`);
  
  // Run an immediate poll to set initial state
  pollResources();
  
  // Set interval (drifts are OK as per requirements)
  intervalId = setInterval(pollResources, POLL_INTERVAL_MS);
}

export function stopMonitor() {
  if (intervalId !== null) {
    clearInterval(intervalId);
    intervalId = null;
    currentLevel = null;
    currentFps = -1;
    log.info('Resource monitor stopped.');
  }
}

export function getResourceSnapshot() {
  return { ..._snapshot };
}
