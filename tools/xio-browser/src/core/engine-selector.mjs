// ─── Engine Selector (UCB1 + Time Decay) ──────────────────────────────────
// Adaptively selects and orders stealth engines per workflow+domain based on
// historical success rates. Uses UCB1 algorithm to balance:
//   - Exploitation: use what worked before
//   - Exploration:  occasionally test other engines to detect regressions
//
// Time decay (λ=0.1, half-life ~7 days) ensures stale successes fade out so
// the system self-heals when a Chrome update breaks an engine.

import { getDb } from './db.mjs';
import { createLogger } from '../utils/logger.mjs';

const log = createLogger('engine-selector');

// ── Constants ─────────────────────────────────────────────────────────────────

// ⚠️  UC-ONLY MODE — camoufox and nodriver are disabled.
// To re-enable other engines: restore ALL_ENGINES and remove the early-return in getRankedEngines.
export const ALL_ENGINES       = ['uc'];
export const DISABLED_ENGINES  = ['camoufox', 'nodriver'];

// UCB1 exploration constant — higher = more exploration of untested engines
const UCB1_C = 0.5;

// Time decay rate (λ): 0.1 → half-life ≈ 7 days
// A win 7 days ago counts as 0.5 wins; 14 days ago = 0.25 wins
const DECAY_LAMBDA = 0.1;

const SEC_PER_DAY = 86400;

// ── Core Scoring ──────────────────────────────────────────────────────────────

/**
 * UCB1 score with time-decayed success rate.
 *
 * Score(engine) =
 *   decayed_success_rate                   (exploitation)
 *   + C × sqrt(ln(N_total) / N_engine)     (exploration bonus)
 *
 * Where decayed_success_rate weights recent wins higher than old ones.
 */
function ucb1Score(row, totalAttempts) {
  const now = Math.floor(Date.now() / 1000);
  const n = row.attempts || 0;

  // Never tried → maximum exploration bonus (treated as 0 attempts)
  if (n === 0) return UCB1_C * Math.sqrt(Math.log(Math.max(totalAttempts, 1) + 1));

  // Time-decayed success rate
  const ageDays = row.last_win_ts
    ? (now - row.last_win_ts) / SEC_PER_DAY
    : Infinity;
  const decayFactor = row.last_win_ts ? Math.exp(-DECAY_LAMBDA * ageDays) : 0;
  const decayedRate = (row.successes / n) * decayFactor;

  // UCB1 exploration bonus
  const explorationBonus = UCB1_C * Math.sqrt(Math.log(totalAttempts + 1) / n);

  return decayedRate + explorationBonus;
}

// ── Public API ────────────────────────────────────────────────────────────────

/**
 * Returns engines sorted by UCB1 score (best first) for the given workflow+domain.
 * Falls back to ALL_ENGINES default order if DB is not available.
 *
 * @param {string} workflowId  - e.g. 'google-signin'
 * @param {string} domain      - e.g. 'accounts.google.com'
 * @returns {string[]}         - ordered engine names
 */
export function getRankedEngines(workflowId, domain = 'any') {
  // UC-ONLY MODE: always return ['uc'] regardless of ESR history.
  // ESR scoring is bypassed to avoid probing disabled engines.
  log.info(`Engine order for ${workflowId}::${domain}: uc (UC-only mode)`);
  return ['uc'];
}

/**
 * Record the outcome of an engine attempt.
 * Call this after EVERY engine attempt, whether success or failure.
 *
 * @param {string} workflowId
 * @param {string} domain
 * @param {string} engine     - one of ALL_ENGINES
 * @param {boolean} success
 */
export function recordEngineResult(workflowId, domain = 'any', engine, success) {
  try {
    const db = getDb();
    const now = Math.floor(Date.now() / 1000);
    db.prepare(`
      INSERT INTO engine_stats (workflow_id, domain, engine, attempts, successes, last_win_ts, last_try_ts)
      VALUES (@workflow_id, @domain, @engine, 1, @s, @win_ts, @now)
      ON CONFLICT(workflow_id, domain, engine) DO UPDATE SET
        attempts    = attempts + 1,
        successes   = successes + @s,
        last_win_ts = CASE WHEN @s = 1 THEN @now ELSE last_win_ts END,
        last_try_ts = @now
    `).run({
      workflow_id: workflowId,
      domain,
      engine,
      s: success ? 1 : 0,
      win_ts: success ? now : null,
      now,
    });
    log.info(`ESR recorded: ${engine} ${success ? '✅' : '❌'} for ${workflowId}::${domain}`);
  } catch (e) {
    log.warn(`ESR record failed (${e.message}) — non-fatal`);
  }
}

/**
 * Returns the engine stats summary for a workflow+domain (for debugging/UI).
 */
export function getEngineStats(workflowId, domain = 'any') {
  try {
    return getDb().prepare(`
      SELECT engine, attempts, successes,
             ROUND(CAST(successes AS REAL) / MAX(attempts,1) * 100, 1) AS success_pct,
             datetime(last_win_ts, 'unixepoch') AS last_win,
             datetime(last_try_ts, 'unixepoch') AS last_try
      FROM engine_stats
      WHERE workflow_id = ? AND domain = ?
      ORDER BY success_pct DESC
    `).all(workflowId, domain);
  } catch {
    return [];
  }
}
