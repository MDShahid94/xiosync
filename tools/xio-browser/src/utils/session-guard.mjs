// ─── Session Guard ─────────────────────────────────────────────────────────
// Shared utility for any workflow that requires a pre-existing Google session.
// Call assertGoogleSession(ctx) at the top of any workflow that opens Colab
// or any Google property that needs the user already signed in.
//
// Usage in a workflow:
//   import { assertGoogleSession } from '../src/utils/session-guard.mjs';
//   await assertGoogleSession(ctx);                     // any account
//   await assertGoogleSession(ctx, 'user@gmail.com');  // specific account

/**
 * Navigate to myaccount.google.com and verify the session is authenticated.
 * Throws a descriptive error if not logged in — prevents silent failures.
 *
 * @param {object} ctx           - workflow context (must have ctx.page and ctx.log)
 * @param {string} [expectedEmail] - if supplied, also verifies the signed-in account
 */
export async function assertGoogleSession(ctx, expectedEmail = null) {
  ctx.log('[session-guard] Verifying Google session...');

  await ctx.page.goto('https://myaccount.google.com/', {
    waitUntil: 'domcontentloaded',
    timeout:   20_000,
  });

  const url = ctx.page.url();

  // Redirect to sign-in means the session is invalid / not present
  if (url.includes('accounts.google.com') || url.includes('/signin') || url.includes('/ServiceLogin')) {
    throw new Error(
      '[session-guard] ❌ Not signed in to Google. ' +
      'Run google-signin first to establish a valid session for this slot.'
    );
  }

  // If we need a specific account, verify via the page title or profile email
  if (expectedEmail) {
    const pageContent = await ctx.page.content();
    const norm = (s) => s.toLowerCase().replace(/\./g, '');
    if (!norm(pageContent).includes(norm(expectedEmail.split('@')[0]))) {
      throw new Error(
        `[session-guard] ❌ Wrong account signed in. Expected: ${expectedEmail}. ` +
        `Current URL: ${url}. Run google-signin for the correct account.`
      );
    }
    ctx.log(`[session-guard] ✅ Signed in as ${expectedEmail}`);
  } else {
    ctx.log(`[session-guard] ✅ Google session valid (URL: ${url.slice(0, 60)})`);
  }
}
