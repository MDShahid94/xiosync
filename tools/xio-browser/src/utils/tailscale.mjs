// ─── Tailscale Utilities ───────────────────────────────────────────────────
// setExitNode(ip)     → runs `sudo tailscale set --exit-node=<ip>`
// getPeerProfile(ip)  → reads `tailscale status --json`, builds browser fingerprint
// selfIP()            → returns this node's Tailscale IP

import { execSync } from 'node:child_process';
import { createLogger } from './logger.mjs';

const log = createLogger('tailscale');

// ── Runtime Default Exit Node ─────────────────────────────────────────────
// Set once from config at boot, overridable at runtime via setDefaultExitNode().
// This is the NETWORK exit node — separate from the CLIENT node (AI agent's device).
let _runtimeDefaultExitNode = null;

export function setDefaultExitNode(ip) {
  _runtimeDefaultExitNode = ip;
  log.info(`Runtime default exit node updated → ${ip}`);
}

export function getDefaultExitNode() {
  return _runtimeDefaultExitNode;
}

// ── Real Device Specs ──────────────────────────────────────────────────────
// Injected at boot from CONFIG.mac_specs (set in start.ipynb Cell 1).
// buildProfileFromPeer() uses these instead of hardcoded fallback values so
// the Chrome fingerprint reflects the actual hardware, not a generic M2 guess.
let _deviceSpecs = null;

/**
 * Called by mcp-server.mjs at boot with the mac_specs from CONFIG.
 * Shape: { cores, ram, webgl_renderer, macos_version, width, height }
 */
export function setDeviceSpecs(specs) {
  _deviceSpecs = specs;
  log.info(`Real device specs loaded: ${JSON.stringify(specs)}`);
}

export function getDeviceSpecs() {
  return _deviceSpecs;
}

// ── Exit Node ──────────────────────────────────────────────────────────────

export async function setExitNode(ip) {
  if (!ip) {
    try {
      log.info('Clearing exit node');
      execSync('sudo tailscale set --exit-node=', { stdio: 'pipe' });
    } catch (e) {
      log.warn(`Could not clear exit node (non-fatal): ${e.stderr?.toString().trim() || e.message}`);
    }
    return;
  }
  try {
    log.info(`Setting exit node → ${ip}`);
    execSync(`sudo tailscale set --exit-node=${ip}`, { stdio: 'pipe' });
    // Give tailscale 2s to re-route before verifying
    await new Promise(r => setTimeout(r, 2000));

    // ── B1 FIX: Verify exit IP is actually an Airtel PPPoE IP ─────────────
    // If this check fails, browser MUST NOT open — fingerprint would claim Mac
    // but IP would be Google/Colab datacenter → instant detection.
    let exitIP = null;
    try {
      const resp = await fetch('https://api.ipify.org', {
        signal: AbortSignal.timeout(6000),
      });
      exitIP = (await resp.text()).trim();
    } catch (fetchErr) {
      throw new Error(`Exit IP verification fetch failed: ${fetchErr.message}`);
    }

    const isAirtel = exitIP && (
      exitIP.startsWith('223.181.') ||  // primary Airtel PPPoE CGNAT range (confirmed live)
      exitIP.startsWith('97.28.')        // secondary Airtel pool (ppp999 anomaly, confirmed)
    );

    if (!isAirtel) {
      throw new Error(
        `EXIT NODE MISMATCH: expected Airtel IP (223.181.x.x / 97.28.x.x), ` +
        `got ${exitIP}. Tailscale may be in userspace fallback or exit node ` +
        `unreachable. Refusing to open browser to prevent fingerprint leak.`
      );
    }
    log.info(`✅ Exit node verified: ${exitIP} via ${ip}`);
    // ─────────────────────────────────────────────────────────────────────────

  } catch (e) {
    // Changed from warn+continue to HARD THROW — no silent bypass
    throw new Error(`FATAL setExitNode(${ip}): ${e.message}`);
  }
}



export function selfIP() {
  try {
    return execSync('tailscale ip -4', { stdio: 'pipe' }).toString().trim();
  } catch {
    return null;
  }
}

// ── Peer Status ────────────────────────────────────────────────────────────

export function getPeerStatus() {
  try {
    const raw = execSync('tailscale status --json', { stdio: 'pipe' }).toString();
    return JSON.parse(raw);
  } catch (e) {
    log.warn(`tailscale status failed: ${e.message}`);
    return null;
  }
}

/**
 * Returns a list of all Tailscale peers suitable for use as exit nodes.
 * Includes: hostname, IPs, OS, current exit_node flag, online status.
 */
export function listAvailablePeers() {
  const status = getPeerStatus();
  if (!status?.Peer) return [];
  return Object.values(status.Peer).map(p => ({
    hostname:      p.HostName,
    tailscale_ips: p.TailscaleIPs ?? [],
    primary_ip:    (p.TailscaleIPs ?? [])[0] ?? null,
    os:            p.OS,
    is_exit_node:  p.ExitNode ?? false,
    online:        p.Online ?? false,
    is_current_default: (p.TailscaleIPs ?? []).includes(_runtimeDefaultExitNode),
  }));
}

export function ensureDaemonRunning() {
  try {
    execSync('tailscale status', { stdio: 'pipe' });
  } catch {
    log.warn('Tailscale daemon offline — restarting');
    try { execSync('sudo pkill -9 tailscaled', { stdio: 'pipe' }); } catch {}
    try { execSync('sudo rm -f /var/run/tailscale/tailscaled.sock', { stdio: 'pipe' }); } catch {}

    // Try kernel tun (full routing + inbound SSH), fall back to userspace
    const tunAvailable = (() => {
      try { execSync('sudo modprobe tun 2>/dev/null', { stdio: 'pipe', shell: true }); return true; }
      catch { return false; }
    })();

    const daemonCmd = tunAvailable
      ? 'nohup sudo tailscaled > /tmp/tailscaled.log 2>&1 &'
      : 'nohup sudo tailscaled --tun=userspace-networking --socks5-server=localhost:1055 > /tmp/tailscaled.log 2>&1 &';

    execSync(daemonCmd, { stdio: 'pipe', shell: true });
    execSync('sleep 4', { stdio: 'pipe', shell: true });
    log.info(`Tailscale daemon restarted (${tunAvailable ? 'kernel' : 'userspace'} mode)`);
  }
}

// ── Fingerprint Builder ────────────────────────────────────────────────────
// Reads the exit node peer's OS + arch from tailscale status --json and
// returns a full browser identity profile that Colab Chrome will impersonate.

// D2 FIX: XIOSYNC-assigned profile takes priority over auto-detection.
// Auto-detection always returns 'linux' for VM peers (Tailscale reports VM's
// Ubuntu OS, NOT the Mac Mini OS being impersonated) → wrong fingerprint.
// setD1FingerprintProfile() is called by boot.py after XIOSYNC acquisition.
let _d1FingerprintProfile = null;
export function setD1FingerprintProfile(profile) {
  _d1FingerprintProfile = profile;
  log.info(`D1 fingerprint profile set: ${profile?.name ?? 'null'}`);
}

export function buildProfileFromPeer(exitNodeIP) {
  // Priority 1: XIOSYNC-assigned fingerprint (most authoritative — real slot profile)
  if (_d1FingerprintProfile) {
    return _buildFromXiosyncProfile(_d1FingerprintProfile);
  }

  const status = getPeerStatus();

  let osType   = 'linux';
  let archType = 'amd64';

  if (status?.Peer) {
    const peer = Object.values(status.Peer).find(p =>
      (p.TailscaleIPs ?? []).includes(exitNodeIP)
    );
    if (peer) {
      osType   = (peer.OS || 'linux').toLowerCase();
      archType = (peer.Arch || 'arm64').toLowerCase();
      log.info(`Exit node ${exitNodeIP} detected as ${peer.OS} (${osType})`);
    }
  }

  // Chrome version dynamically read from installed Chrome (Colab has it installed)
  let cvFull = '131.0.0.0';
  let cvMajor = '131';
  try {
    const raw = execSync('google-chrome --version 2>/dev/null || chromium --version 2>/dev/null', { shell: true, stdio: 'pipe' }).toString().trim();
    const match = raw.match(/[\d.]+/);
    if (match) { cvFull = match[0]; cvMajor = cvFull.split('.')[0]; }
  } catch { /* use defaults */ }

  // Profile selection by OS + arch
  if (osType === 'macos' || osType === 'darwin') {
    // Use real device specs if injected at boot (from CONFIG.mac_specs in start.ipynb)
    // Falls back to safe sensible defaults when specs not provided.
    const sp = _deviceSpecs ?? {};
    const cores    = sp.cores           ?? 10;          // M4: 6P+4E
    const ram      = sp.ram             ?? 16;          // GB unified memory
    const renderer = sp.webgl_renderer  ?? 'Apple M4';  // actual chip generation
    const macVer   = sp.macos_version   ?? '15.2.0';    // Sequoia 15.2
    const width    = sp.width           ?? 2560;        // Mac Mini M4 default display
    const height   = sp.height          ?? 1440;
    log.info(`Applying macOS fingerprint profile for exit node ${exitNodeIP} (${renderer}, ${cores}c/${ram}GB, ${macVer})`);
    return {
      // NOTE: 'Intel Mac OS X 10_15_7' in the UA is intentional — Chrome on Apple Silicon
      // (M1/M2/M4) also reports this string. macOS version in UA is frozen at 10_15_7
      // by Chrome for all Mac variants for privacy reasons. chArch:'arm' is the correct
      // UACH high-entropy value and does NOT contradict the UA string.
      userAgent:  `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${cvFull} Safari/537.36`,
      platform:   'MacIntel',   // navigator.platform — Chrome reports this on all Macs
      chPlatform: 'macOS',
      chVersion:  macVer,
      chArch:     'arm',        // UACH architecture (correct for Apple Silicon)
      cvFull, cvMajor,
      isMobile:   false,
      width, height,
      ram, cores,
      webglVendor:   'Apple Inc.',
      webglRenderer: renderer,
      camName: 'FaceTime HD Camera',
    };
  }

  if (osType === 'android') {
    // Android mobile
    return {
      userAgent:  `Mozilla/5.0 (Linux; Android 14; SM-S901B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${cvFull} Mobile Safari/537.36`,
      platform:   'Linux armv81',
      chPlatform: 'Android',
      chVersion:  '14.0.0',
      chArch:     'arm',
      cvFull, cvMajor,
      isMobile:   true,
      width: 412, height: 915,
      ram: 8, cores: 8,
      webglVendor:   'Qualcomm',
      webglRenderer: 'Adreno (TM) 730',
      camName: 'Front Camera',
    };
  }

  if (osType === 'windows') {
    return {
      userAgent:  `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${cvFull} Safari/537.36`,
      platform:   'Win32',
      chPlatform: 'Windows',
      chVersion:  '10.0.0',
      chArch:     'x86',
      cvFull, cvMajor,
      isMobile:   false,
      width: 1920, height: 1080,
      ram: 16, cores: 8,
      webglVendor:   'Google Inc. (Intel)',
      webglRenderer: 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)',
      camName: 'Integrated Webcam',
    };
  }

  // Default: Linux desktop
  return {
    userAgent:  `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${cvFull} Safari/537.36`,
    platform:   'Linux x86_64',
    chPlatform: 'Linux',
    chVersion:  '5.15.0',
    chArch:     'x86',
    cvFull, cvMajor,
    isMobile:   false,
    width: 1920, height: 1080,
    ram: 8, cores: 4,
    webglVendor:   'Mesa/X.org',
    webglRenderer: 'llvmpipe (LLVM 15.0.7, 256 bits)',
    camName: 'USB Camera',
    canvasSeed: 0, audioSeed: 0, dpr: 1,
  };
}

// ── XIOSYNC profile converter ──────────────────────────────────────────────
// Converts a FingerprintRecord (from /api/v1/pppoe/fingerprints/{host}/{slot})
// into the profile shape that applyFingerprintOverrides() consumes.
function _buildFromXiosyncProfile(fp) {
  // Get Chrome version from installed Chrome (same logic as buildProfileFromPeer)
  let cvFull = '131.0.0.0';
  let cvMajor = '131';
  try {
    const raw = execSync('google-chrome --version 2>/dev/null || chromium --version 2>/dev/null',
      { shell: true, stdio: 'pipe' }).toString().trim();
    const match = raw.match(/[\d.]+/);
    if (match) { cvFull = match[0]; cvMajor = cvFull.split('.')[0]; }
  } catch { /* use defaults */ }

  // Expand ua_template: replace {cv} with actual Chrome version
  const userAgent = (fp.ua_template || '').replace('{cv}', cvFull);

  return {
    userAgent,
    platform:      fp.platform,
    chPlatform:    fp.ch_platform,
    chVersion:     fp.ch_version || cvFull,
    chArch:        fp.ch_arch,
    cvFull,
    cvMajor:       parseInt(cvMajor, 10) || 131,
    isMobile:      fp.is_mobile ?? false,
    width:         fp.screen_width,
    height:        fp.screen_height,
    dpr:           fp.dpr ?? 1,
    ram:           fp.ram_gb,
    cores:         fp.cores,
    webglVendor:   fp.webgl_vendor   ?? 'Apple Inc.',
    webglRenderer: fp.webgl_renderer ?? 'Apple M4',
    camName:       fp.cam_name       ?? 'FaceTime HD Camera',
    canvasSeed:    fp.canvas_seed    ?? 0,
    audioSeed:     fp.audio_seed     ?? 0,
  };
}

