// ─── Logger ────────────────────────────────────────────────────────────────
const LEVELS = { DEBUG: 0, INFO: 1, WARN: 2, ERROR: 3 };
const MIN_LEVEL = LEVELS[process.env.XIOBR_LOG_LEVEL?.toUpperCase()] ?? LEVELS.INFO;

const ICONS = { DEBUG: '🔍', INFO: 'ℹ️ ', WARN: '⚠️ ', ERROR: '❌' };

function emit(ns, level, ...args) {
  if (LEVELS[level] < MIN_LEVEL) return;
  const ts = new Date().toISOString();
  process.stderr.write(`${ICONS[level]} [${ts}] [${ns}] ${args.join(' ')}\n`);
}

export function createLogger(ns) {
  return {
    debug: (...a) => emit(ns, 'DEBUG', ...a),
    info:  (...a) => emit(ns, 'INFO',  ...a),
    warn:  (...a) => emit(ns, 'WARN',  ...a),
    error: (...a) => emit(ns, 'ERROR', ...a),
  };
}
