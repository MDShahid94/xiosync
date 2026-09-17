/**
 * disconnect-all.mjs — Kill one, several, or all Colab runtimes in the mesh
 *
 * Flow:
 *   1. discover   — tailscale status --json → all colab-* peers;
 *                   resolve each `targets` entry (IP / hostname / slug) to a peer
 *   2. kill_each  — parallel SSH pkill -9 (node, python3, jupyter, ipykernel,
 *                   ecosystem.mjs). Clears stale known_hosts before each SSH.
 *                   Falls back to MCP-shell (POST :4242/call xb_run_shell) if SSH fails.
 *   3. verify     — poll each :4242/health every 3s up to verifyTimeout;
 *                   explicitly confirm dead vs still_alive per node
 *   4. report     — final ASCII table: hostname / IP / DEAD | STILL ALIVE
 *                   Throws if any targeted runtime survived (job fails visibly).
 *
 * Params:
 *   targets  — string | string[]
 *              Each entry may be any of:
 *                • Tailscale IP            "100.120.53.61"
 *                • Full hostname           "colab-master-1"
 *                • Partial slug (substr)   "master", "1x2xx", "samnur", "shahid-workload"
 *                • Account username        "samnurnihartalukdar", "shahid.workload"
 *              null/omitted → ALL colab-* peers (original behaviour)
 *   skip     — string | string[] — IPs/hostnames/slugs to exclude (same resolution)
 *   sshTimeout    — SSH connect timeout seconds (default 6)
 *   verifyTimeout — max seconds to wait per-IP for MCP to die (default 30)
 *   dryRun        — log what would be killed without actually killing
 *
 * Examples:
 *   // Kill everyone
 *   xb_run_workflow({ workflow: 'disconnect-all', session_id: '...' })
 *
 *   // Kill one node by slug
 *   xb_run_workflow({ workflow: 'disconnect-all', session_id: '...',
 *                     params: { targets: 'samnur' } })
 *
 *   // Kill two nodes by account name + IP, skip master
 *   xb_run_workflow({ workflow: 'disconnect-all', session_id: '...',
 *                     params: {
 *                       targets: ['samnurnihartalukdar', '100.121.186.123'],
 *                       skip:    'master',
 *                     } })
 *
 *   // Dry-run to see what would be killed
 *   xb_run_workflow({ workflow: 'disconnect-all', session_id: '...',
 *                     params: { dryRun: true } })
 */

import { execSync } from 'node:child_process';
import { promisify } from 'node:util';
import { exec }      from 'node:child_process';
import { mkdirSync } from 'node:fs';
const execP = promisify(exec);

// ── Resolve targets/skip entries to Tailscale IPs ────────────────────────────
// Each entry may be an IP (exact), a hostname (exact or prefix-match),
// or a slug that appears anywhere in the hostname (case-insensitive substring).
// Also strips dots so "shahid.workload" matches "colab-worker-shahid-workload".
function resolveToIPs(entries, allPeers) {
  if (!entries) return null; // null = no filter (take all)
  const list = Array.isArray(entries) ? entries : [entries];
  const resolved = new Set();
  for (const entry of list) {
    const norm = entry.toLowerCase().replace(/\./g, '-'); // "shahid.workload" → "shahid-workload"
    for (const p of allPeers) {
      if (
        p.ip === entry ||                                         // exact IP
        p.hostname.toLowerCase() === entry.toLowerCase() ||       // exact hostname
        p.hostname.toLowerCase().includes(norm)                   // slug / account-name substring
      ) {
        resolved.add(p.ip);
      }
    }
    if (resolved.size === 0) {
      // Warn — entry didn't match anything; don't silently ignore
    }
  }
  return resolved;
}

export async function run(ctx, params = {}) {
  let {
    targets       = null,   // null = all colab-* peers
    skip          = [],     // nodes to leave alone
    sshTimeout    = 6,
    verifyTimeout = 30,
    dryRun        = false,
  } = params;

  const _jobDir = ctx.jobDir ?? `/tmp/disconnect-all-${Date.now()}`;
  mkdirSync(`${_jobDir}/steps`, { recursive: true });

  const targetsLabel = targets == null
    ? 'ALL colab-* nodes'
    : JSON.stringify(Array.isArray(targets) ? targets : [targets]);
  ctx.log(`[disconnect-all] targets=${targetsLabel}  skip=${JSON.stringify(skip)}  dryRun=${dryRun}`);

  // ── Step 1: discover ─────────────────────────────────────────────────────────
  let peers = [];
  await ctx.step('discover', async () => {
    const tsJson = execSync('tailscale status --json 2>/dev/null',
      { encoding: 'utf8', timeout: 10_000 });
    const tsParsed = JSON.parse(tsJson);
    const tsPeers  = tsParsed.Peer ?? {};

    // Build the full peer list first (needed for resolution)
    const allPeers = [];
    for (const [, p] of Object.entries(tsPeers)) {
      const hn  = p.HostName ?? '';
      const ips = p.TailscaleIPs ?? [];
      if (!hn.startsWith('colab-') || ips.length === 0) continue;
      allPeers.push({ ip: ips[0], hostname: hn, online: p.Online ?? false });
    }
    // Include self (not in Peer map)
    try {
      const selfIp = tsParsed.TailscaleIPs?.[0] ?? null;
      const selfHn = tsParsed.Self?.HostName ?? (process.env.XIO_NODE_NAME ?? '');
      if (selfIp && selfHn.startsWith('colab-')) {
        allPeers.push({ ip: selfIp, hostname: selfHn, online: true, isSelf: true });
      }
    } catch (_) {}

    // Resolve targets and skip filters
    const targetIPs = resolveToIPs(targets, allPeers);
    const skipIPs   = resolveToIPs(skip, allPeers) ?? new Set();

    // Warn about unresolved targets
    if (targets !== null) {
      const list = Array.isArray(targets) ? targets : [targets];
      for (const entry of list) {
        const norm = entry.toLowerCase().replace(/\./g, '-');
        const hit  = allPeers.some(p =>
          p.ip === entry || p.hostname.toLowerCase() === entry.toLowerCase() ||
          p.hostname.toLowerCase().includes(norm)
        );
        if (!hit) ctx.log(`  ⚠️  [discover] target "${entry}" did not match any colab-* peer`);
      }
    }

    // Apply filters
    for (const p of allPeers) {
      if (skipIPs.has(p.ip)) {
        ctx.log(`  ⏭  skip ${p.hostname} (${p.ip})`);
        continue;
      }
      if (targetIPs !== null && !targetIPs.has(p.ip)) continue;
      peers.push(p);
    }

    ctx.log(`[discover] Targeting ${peers.length} of ${allPeers.length} runtime(s):`);
    for (const p of peers)
      ctx.log(`  ${p.online ? '🟢' : '⚪'} ${p.hostname.padEnd(50)} ${p.ip}`);
  });

  if (peers.length === 0) {
    ctx.log('[disconnect-all] ✅ No matching runtimes found — nothing to kill.');
    return;
  }

  // ── Step 2: kill_each (parallel) ─────────────────────────────────────────────
  const killResults = {};
  await ctx.step('kill_each', async () => {
    if (dryRun) {
      peers.forEach(p => {
        ctx.log(`  [dry] would kill: ${p.hostname} (${p.ip})`);
        killResults[p.ip] = 'dry_run';
      });
      return;
    }

    const KILL_CMD = [
      `pkill -9 -f 'ecosystem.mjs'  2>/dev/null || true`,
      `pkill -9 -f 'xio-browser'    2>/dev/null || true`,
      `pkill -9 node                 2>/dev/null || true`,
      `pkill -9 -f 'ipykernel'      2>/dev/null || true`,
      `pkill -9 -f 'jupyter'        2>/dev/null || true`,
      `pkill -9 python3              2>/dev/null || true`,
      `echo __KILLED__`,
    ].join('; ');

    const SSH_OPTS = [
      '-o StrictHostKeyChecking=no',
      '-o UserKnownHostsFile=/dev/null',
      `-o ConnectTimeout=${sshTimeout}`,
      '-o ServerAliveInterval=2',
      '-o ServerAliveCountMax=2',
    ].join(' ');

    await Promise.all(peers.map(async ({ ip, hostname }) => {
      // Clear stale known_hosts to prevent host-key-changed blocks
      try { execSync(`ssh-keygen -R ${ip} 2>/dev/null`, { timeout: 2000 }); } catch (_) {}

      // Attempt 1: SSH
      try {
        const { stdout } = await execP(
          `ssh ${SSH_OPTS} root@${ip} '${KILL_CMD}'`,
          { timeout: (sshTimeout + 5) * 1000 }
        );
        if (stdout.includes('__KILLED__')) {
          ctx.log(`  ✅ [ssh]  ${hostname} (${ip}) killed`);
          killResults[ip] = 'killed_ssh';
          return;
        }
      } catch (e) {
        ctx.log(`  ⚠️  [ssh]  ${hostname} (${ip}): ${e.message?.slice(0, 80)}`);
      }

      // Attempt 2: MCP-shell fallback (if the node's MCP is still responding)
      try {
        const res = await fetch(`http://${ip}:4242/call`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ tool: 'xb_run_shell', args: { cmd: KILL_CMD } }),
          signal:  AbortSignal.timeout(8000),
        });
        if (res.ok) {
          ctx.log(`  ✅ [mcp]  ${hostname} (${ip}) killed via MCP-shell`);
          killResults[ip] = 'killed_mcp';
          return;
        }
      } catch (e) {
        ctx.log(`  ⚠️  [mcp]  ${hostname} (${ip}): ${e.message?.slice(0, 80)}`);
      }

      ctx.log(`  ❌ [fail] ${hostname} (${ip}) — SSH + MCP-shell both failed`);
      killResults[ip] = 'kill_failed';
    }));
  });

  // ── Step 3: verify ───────────────────────────────────────────────────────────
  const verifyResults = {};
  await ctx.step('verify', async () => {
    if (dryRun) {
      peers.forEach(p => { verifyResults[p.ip] = 'dry_run'; });
      return;
    }

    const remaining = new Set(peers.map(p => p.ip));
    const deadline  = Date.now() + verifyTimeout * 1000;
    ctx.log(`[verify] Polling ${remaining.size} node(s) for up to ${verifyTimeout}s…`);

    while (remaining.size > 0 && Date.now() < deadline) {
      await Promise.all([...remaining].map(async (ip) => {
        try {
          const res = await fetch(`http://${ip}:4242/health`,
            { signal: AbortSignal.timeout(2500) });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          // still alive — keep in set
        } catch (_) {
          remaining.delete(ip);
          verifyResults[ip] = 'dead';
          const hn = peers.find(p => p.ip === ip)?.hostname ?? ip;
          ctx.log(`  ✅ ${hn} (${ip}) confirmed dead`);
        }
      }));
      if (remaining.size > 0) await new Promise(r => setTimeout(r, 3000));
    }

    for (const ip of remaining) {
      verifyResults[ip] = 'still_alive';
      const hn = peers.find(p => p.ip === ip)?.hostname ?? ip;
      ctx.log(`  ❌ ${hn} (${ip}) STILL ALIVE after ${verifyTimeout}s`);
    }
  });

  // ── Step 4: report ───────────────────────────────────────────────────────────
  await ctx.step('report', async () => {
    const SEP = '═'.repeat(66);
    ctx.log(`\n╔${SEP}╗`);
    ctx.log(`║${'  disconnect-all — FINAL STATUS'.padEnd(66)}║`);
    ctx.log(`╠${SEP}╣`);

    let allDead = true;
    for (const { ip, hostname } of peers) {
      const killHow = killResults[ip]   ?? '—';
      const verify  = verifyResults[ip] ?? 'not_checked';
      const dead    = verify === 'dead' || verify === 'dry_run';
      if (!dead) allDead = false;
      const icon  = dead ? '✅' : '❌';
      const state = dead ? `DEAD (${killHow})` : 'STILL ALIVE ⚠️';
      ctx.log(`║  ${icon}  ${hostname.slice(0, 38).padEnd(38)}  ${ip.padEnd(15)}  ${state.padEnd(18)}  ║`);
    }

    ctx.log(`╠${SEP}╣`);
    const summary = allDead
      ? `✅ All ${peers.length} targeted runtime(s) confirmed dead`
      : `⚠️  Some runtime(s) still alive — manual intervention needed`;
    ctx.log(`║  ${summary.padEnd(64)}║`);
    ctx.log(`╚${SEP}╝`);

    if (!allDead) {
      throw new Error('Some targeted runtimes still alive after kill+verify. See report above.');
    }
  });
}
