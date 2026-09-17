// ─── Session ID Normalisation ─────────────────────────────────────────────────────
// Canonical form for all session IDs is the SLUG (e.g. 'shahid.raiganj').
// The DB sessions table uses slug IDs — email-alias rows were legacy and are
// now purged. Drive files use PRFL-NNN_slug form derived from the slug.
//
// Callers may pass:
//   slug with dots:       "shahid.raiganj"           → "shahid.raiganj"   (passthrough)
//   slug with underscores:"shahid_raiganj"           → "shahid.raiganj"   (normalize)
//   full email:           "shahid.raiganj@gmail.com" → "shahid.raiganj"   (strip domain)

/**
 * Normalise any session_id input to its canonical slug form.
 *
 * Accepts:
 *   "shahid.raiganj"           → "shahid.raiganj"   (passthrough)
 *   "shahid_raiganj"           → "shahid.raiganj"   (underscore → dot)
 *   "shahid.raiganj@gmail.com" → "shahid.raiganj"   (strip @domain)
 *   "user@company.com"         → "user"              (strip @domain)
 *   null / undefined            → null / undefined   (passthrough)
 *
 * Rules:
 *   1. If contains '@' → strip the @domain suffix to get the slug.
 *   2. Replace remaining underscores with dots (legacy slug normalisation).
 *
 * @param {string|null|undefined} id
 * @returns {string|null|undefined}
 */
export function normaliseSessionId(id) {
  if (!id) return id;
  // Strip email domain — e.g. 'shahid.raiganj@gmail.com' → 'shahid.raiganj'
  const slug = id.includes('@') ? id.replace(/@[^@]+$/, '') : id;
  // Normalise underscore → dot for legacy slugs like 'shahid_raiganj'
  return slug.replace(/_/g, '.');
}

/**
 * Returns true if the session_id is already in canonical slug form.
 * (i.e., does NOT contain '@' and has no underscores)
 * @param {string} id
 * @returns {boolean}
 */
export function isCanonicalSessionId(id) {
  return typeof id === 'string' && !id.includes('@') && !id.includes('_');
}
