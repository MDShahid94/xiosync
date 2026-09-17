/**
 * ecosystem.mjs — XIO Mesh account/network/role taxonomy
 *
 * Single source of truth for:
 *   - Domain registry  (google, tailscale, v0, colab, ...)
 *   - Account tiers    (Pro / Starter) and their spawn eligibility
 *   - Named networks   (tailnet-primary with mesh-admin + mesh-clients)
 *   - Spawn-eligible tiers (config-driven for future extensibility)
 *
 * All values are config-driven via xio_config.json where applicable,
 * with sane defaults so the system works without explicit configuration.
 *
 * Usage:
 *   import { getMeshAdmin, isSpawnEligible, getDomain, NETWORKS }
 *     from '../src/core/ecosystem.mjs';
 *
 * Future extensibility:
 *   - Add a new domain:   DOMAINS['github'] = { ... }
 *   - Add a new network:  NETWORKS['tailnet-secondary'] = { ... }
 *   - Allow Starter tier: set spawn_eligible_tiers: ['Pro','Starter'] in xio_config.json
 */

import fs from 'node:fs';
import { createLogger } from '../utils/logger.mjs';
import { listAccounts } from './db.mjs';

const log = createLogger('ecosystem');

// ── Domain registry ────────────────────────────────────────────────────────
/**
 * Each domain entry describes how to authenticate and verify sessions
 * for a specific third-party service.
 *
 * @type {Record<string, {
 *   id: string,
 *   label: string,
 *   signinWorkflow: string|null,
 *   verifierModule: string|null,
 *   cookieApex: string,
 *   rotatingTokens: string[],
 *   tokenMaxAgeHrs: number,
 *   midAuthUrlFragments: string[],
 * }>}
 */
export const DOMAINS = {
  google: {
    id:                  'google',
    label:               'Google Accounts',
    signinWorkflow:      'google-signin',
    verifierModule:      'src/core/session-verifier.mjs',
    cookieApex:          'google.com',
    // Rotating tokens that expire after ~24h of inactivity.
    // If any of these change value (same key but new value), the session
    // may be invalidated by Google — do not save mid-auth states.
    rotatingTokens:      ['SIDCC', '__Secure-1PSIDTS', '__Secure-3PSIDTS'],
    tokenMaxAgeHrs:      24,
    // URL fragments that indicate an active OAuth redirect / challenge.
    // saveStorageState() skips Google-cookie updates when the page is here.
    midAuthUrlFragments: [
      'accounts.google.com/v3/signin',
      'accounts.google.com/signin/v2',
      'accounts.google.com/o/oauth2',
      'accounts.google.com/CheckCookie',
    ],
  },
  tailscale: {
    id:                  'tailscale',
    label:               'Tailscale VPN',
    signinWorkflow:      'tailscale-signin',
    verifierModule:      null,   // verified by console.tailscale.com URL check
    cookieApex:          'tailscale.com',
    rotatingTokens:      [],
    tokenMaxAgeHrs:      0,      // session-lifetime tokens — no rotation
    midAuthUrlFragments: [
      'login.tailscale.com',
      'login.tailscale.com/a/',
    ],
  },
  v0: {
    id:                  'v0',
    label:               'v0 by Vercel',
    signinWorkflow:      'v0-signin',
    verifierModule:      null,
    cookieApex:          'v0.dev',
    rotatingTokens:      [],
    tokenMaxAgeHrs:      0,
    midAuthUrlFragments: [],
  },
  colab: {
    id:                  'colab',
    label:               'Google Colab',
    signinWorkflow:      null,   // inherits Google session — no standalone signin
    verifierModule:      null,
    cookieApex:          'google.com',
    rotatingTokens:      [],
    tokenMaxAgeHrs:      0,
    midAuthUrlFragments: [],
  },
};

/**
 * Get domain config by id.
 * @param {string} domainId
 * @returns {object|null}
 */
export function getDomain(domainId) {
  return DOMAINS[domainId] ?? null;
}

/**
 * Returns all URL fragments that indicate a mid-auth state across ALL domains.
 * Used by saveStorageState to detect when it should not overwrite session cookies.
 * @returns {string[]}
 */
export function getAllMidAuthFragments() {
  return Object.values(DOMAINS).flatMap(d => d.midAuthUrlFragments ?? []);
}

// ── Account tier definitions ───────────────────────────────────────────────
/**
 * Tier rules.
 * selfSpawnEligible is the BUILT-IN default; it can be overridden by
 * xio_config.json → spawn_eligible_tiers at runtime.
 *
 * @type {Record<string, { id: string, selfSpawnEligible: boolean, maxConcurrentJobs: number }>}
 */
export const TIERS = {
  Pro: {
    id:                   'Pro',
    selfSpawnEligible:    true,   // can open new Colab runtimes
    maxConcurrentJobs:    3,
  },
  Starter: {
    id:                   'Starter',
    selfSpawnEligible:    false,  // worker-only by default
    maxConcurrentJobs:    1,
  },
};

// ── Spawn-eligible tiers (runtime-configurable) ───────────────────────────

let _spawnEligibleTiers = null;

/**
 * Returns the list of tiers that are allowed to run self-spawn.
 * Reads from xio_config.json → spawn_eligible_tiers (default: ['Pro']).
 * Override in start.ipynb: SPAWN_ELIGIBLE_TIERS = ['Pro', 'Starter']
 * @returns {string[]}
 */
export function getSpawnEligibleTiers() {
  if (_spawnEligibleTiers) return _spawnEligibleTiers;
  try {
    const cfg = JSON.parse(fs.readFileSync('/tmp/xio_config.json', 'utf8'));
    _spawnEligibleTiers = cfg.spawn_eligible_tiers ?? ['Pro'];
  } catch {
    _spawnEligibleTiers = ['Pro'];
  }
  return _spawnEligibleTiers;
}

/**
 * Returns true if the given tier is allowed to run self-spawn.
 * @param {string} tier - e.g. 'Pro', 'Starter'
 * @param {'Pro'|'Starter'|'Any'} targetTier - Default: 'Pro'
 * @returns {boolean}
 */
export function isSpawnEligible(tier, targetTier = 'Pro') {
  if (targetTier === 'Any') return true;
  const t = (tier ?? 'starter').toLowerCase();
  if (targetTier.toLowerCase() !== 'pro') {
    return t === targetTier.toLowerCase();
  }
  return getSpawnEligibleTiers()
    .map(x => x.toLowerCase())
    .includes(t);
}

/**
 * Auto-select a spawn-eligible account.
 * @param {Object} opts
 * @param {'Pro'|'Starter'|'Any'} opts.tier - Account tier filter
 * @param {'random'|'ordered'} opts.selection - Selection mode
 * @param {string[]} [opts.exclude] - Session IDs to exclude
 * @param {string} [opts.network] - Mesh network (default: tailnet-primary)
 * @returns {string|null} session_id or null if none available
 */
export function selectSpawnAccount({ tier = 'Pro', selection = 'ordered', exclude = [], network = 'tailnet-primary' } = {}) {
  const accounts = listAccounts().filter(a => a.is_active);
  let eligible = accounts.filter(a => {
    const session_id = a.email.split('@')[0];
    if (exclude.includes(session_id)) return false;
    return isSpawnEligible(a.tier, tier);
  });
  
  if (eligible.length === 0) return null;

  if (selection === 'random') {
    const idx = Math.floor(Math.random() * eligible.length);
    return eligible[idx].email.split('@')[0];
  } else {
    // ordered by serial number extracted from email, fallback to 0
    eligible.sort((a, b) => {
      const matchA = a.email.match(/\d+/);
      const matchB = b.email.match(/\d+/);
      const numA = matchA ? parseInt(matchA[0], 10) : 0;
      const numB = matchB ? parseInt(matchB[0], 10) : 0;
      return numA - numB;
    });
    return eligible[0].email.split('@')[0];
  }
}

// ── Named networks (mesh topologies) ──────────────────────────────────────
/**
 * A "network" is a named mesh topology.
 * Each network has:
 *   - type:    the underlying service (tailscale, wireguard, ...)
 *   - admin:   bare session slug of the mesh-admin account
 *   - clients: resolved dynamically from session_services (not stored here)
 *
 * Multiple networks can coexist; additional networks can be added to
 * xio_config.json → networks or directly here for code-managed networks.
 *
 * @type {Record<string, { id: string, type: string, label: string, admin: string }>}
 */
export const NETWORKS = {
  'tailnet-primary': {
    id:    'tailnet-primary',
    type:  'tailscale',
    label: 'XIO Primary Tailnet',
    // The mesh-admin account manages the Tailscale network and authorizes
    // new devices. This is the ONLY account that runs tailscale-signin and
    // tailscale-auth workflows.
    admin: 'sunmontueswednesthursfrisatur7',
  },
  // Future networks (add here or via xio_config.json → networks):
  // 'tailnet-secondary': { id: 'tailnet-secondary', type: 'tailscale', label: 'XIO Secondary Tailnet', admin: '...' },
};

/**
 * Get the bare session slug for the mesh-admin of the given network.
 * Reads xio_config.json first (allows override without code change).
 *
 * @param {string} [networkId='tailnet-primary']
 * @returns {string} bare session slug (no @gmail.com)
 */
export function getMeshAdmin(networkId = 'tailnet-primary') {
  try {
    const cfg  = JSON.parse(fs.readFileSync('/tmp/xio_config.json', 'utf8'));
    const nets = cfg.networks ?? {};
    if (nets[networkId]?.admin) return nets[networkId].admin.split('@')[0];
  } catch { /* use hardcoded defaults */ }
  return (NETWORKS[networkId]?.admin ?? 'sunmontueswednesthursfrisatur7');
}

/**
 * Get the full email for the mesh-admin of the given network.
 * @param {string} [networkId='tailnet-primary']
 * @returns {string}
 */
export function getMeshAdminEmail(networkId = 'tailnet-primary') {
  const slug = getMeshAdmin(networkId);
  return slug.includes('@') ? slug : `${slug}@gmail.com`;
}

/**
 * Reset cached tiers (for hot-reload / test environments).
 */
export function resetEcosystemCache() {
  _spawnEligibleTiers = null;
}
