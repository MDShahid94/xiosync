/**
 * signin-policy.mjs — Global session-invalid handler policy
 *
 * Controls how all workflows and sub-workflows respond when a Google session
 * is found to be expired. The policy is read from xio_config.json on first
 * access and cached for the lifetime of the process.
 *
 * Modes (set via start.ipynb AUTO_SPAWN_GOOGLE_SIGNIN):
 *
 *   'auto'  (default) ─ Each workflow follows its own built-in default.
 *                        self-spawn: rescue pull → skip to next Pro account.
 *                        google-signin: always re-login.
 *                        Other workflows: pass-through (no intervention).
 *
 *   'skip'  ─ Globally skip ALL auto-signin across every workflow.
 *             Any workflow/sub-workflow uses the next valid profile as-is.
 *             No browser-based re-login is attempted.
 *
 *   'force' ─ Globally force re-signin on ANY expired session before
 *             proceeding. self-spawn will attempt ctx.runInline('google-signin')
 *             on the SAME account before falling back to another.
 *             All sub-workflows also respect this setting.
 *
 * Usage in any workflow:
 *   import { getSigninPolicy, shouldForceSignin, shouldSkipSignin }
 *     from '../src/core/signin-policy.mjs';
 *
 *   if (shouldForceSignin()) { ... }
 *   if (shouldSkipSignin())  { ... }
 */

import fs from 'node:fs';
import { createLogger } from '../utils/logger.mjs';

const log = createLogger('signin-policy');

/** @type {'auto'|'skip'|'force'|null} */
let _mode = null;

/**
 * Returns the current global signin policy mode.
 * Reads from /tmp/xio_config.json once and caches.
 * @returns {'auto'|'skip'|'force'}
 */
export function getSigninPolicy() {
  if (_mode) return _mode;
  try {
    const cfg = JSON.parse(fs.readFileSync('/tmp/xio_config.json', 'utf8'));
    const raw = (cfg.auto_spawn_google_signin ?? 'auto').toLowerCase().trim();
    if (['auto', 'skip', 'force'].includes(raw)) {
      _mode = raw;
    } else {
      log.warn(`signin-policy: unknown mode '${raw}' — defaulting to 'auto'`);
      _mode = 'auto';
    }
  } catch {
    _mode = 'auto';
  }
  log.info(`signin-policy: mode = '${_mode}'`);
  return _mode;
}

/** Returns true when the policy requires forced re-signin before account rotation. */
export function shouldForceSignin() {
  return getSigninPolicy() === 'force';
}

/** Returns true when the policy prohibits any automatic re-signin. */
export function shouldSkipSignin() {
  return getSigninPolicy() === 'skip';
}

/**
 * Resets the cached mode (useful for hot-reload / test environments).
 * Call this if xio_config.json changes at runtime.
 */
export function resetSigninPolicyCache() {
  _mode = null;
}
