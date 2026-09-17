// ─── Workflow: v0-signin-with-google ──────────────────────────────────────
// Signs into v0.app using an existing Google session in the same browser context.
// Because both Google and V0 share the same Chrome context (same slot),
// the Google session cookies are already present — this workflow just clicks
// "Continue with Google" and the OAuth flow completes without re-entering credentials.
//
// Prerequisite: The slot must already have a valid Google session.
//   → Run google-signin first, THEN run this workflow in the same session slot.
//
// This demonstrates the core multi-domain advantage: no credential re-entry,
// no isolated instance switching.

export const meta = {
  id:          'v0-signin-with-google',
  service:     'v0',
  action:      'signin-via-google-oauth',
  description: 'Sign into v0.app using the Google session already in this slot (no credentials needed)',
  params: {},  // No params needed — Google cookies already in context
};

export async function run(ctx, _params) {
  await ctx.step('navigate_to_v0', async () => {
    await ctx.page.goto('https://v0.dev', { waitUntil: 'domcontentloaded' });
  });

  await ctx.step('check_already_logged_in', async () => {
    const url = ctx.page.url();
    // If already logged into V0, we're done
    if (!url.includes('/signin') && !url.includes('/login')) {
      ctx.log(`Already logged into V0: ${url}`);
      ctx.setResult({ success: true, already_logged_in: true, url });
      return;
    }
    ctx.log('Not yet logged in, proceeding to Google OAuth');
  });

  await ctx.step('click_google_signin', async () => {
    // Navigate to signin if not already there
    const url = ctx.page.url();
    if (!url.includes('/signin')) {
      await ctx.page.goto('https://v0.dev/signin', { waitUntil: 'domcontentloaded' });
    }

    // Find and click the "Continue with Google" button
    const googleBtn = await ctx.page.$(
      'button:has-text("Google"), a:has-text("Google"), [data-provider="google"]'
    );
    if (!googleBtn) throw new Error('No Google sign-in button found on v0.dev/signin');

    await googleBtn.click();
    ctx.log('Clicked "Continue with Google"');

    // Wait for the OAuth redirect — Google will recognise the session
    // and either auto-confirm or show the account picker
    await ctx.page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 10000 })
                  .catch(() => {}); // Some flows don't trigger full navigation
    await ctx.page.waitForTimeout(2000);
  });

  await ctx.step('handle_account_picker', async () => {
    const url = ctx.page.url();
    ctx.log(`Post-click URL: ${url}`);

    // Google may show an account chooser — select the first account automatically
    const accountBtn = await ctx.page.$('[data-authuser], [data-identifier], .XbIr3b');
    if (accountBtn) {
      ctx.log('Account picker appeared — selecting first account');
      await accountBtn.click();
      await ctx.page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 10000 })
                    .catch(() => {});
      await ctx.page.waitForTimeout(2000);
    } else {
      ctx.log('No account picker — Google auto-confirmed session');
    }
  });

  await ctx.step('verify', async () => {
    const url = ctx.page.url();
    ctx.log(`Final URL: ${url}`);

    const isLoggedIn = !url.includes('/signin') && !url.includes('/login');
    if (!isLoggedIn) {
      throw new Error(
        `V0 signin via Google may have failed. URL: ${url}. ` +
        `Ensure this slot has a valid Google session first (run google-signin).`
      );
    }

    const cookies = await ctx.page.context().cookies();
    ctx.setResult({ success: true, final_url: url, cookie_count: cookies.length });
  });
}
