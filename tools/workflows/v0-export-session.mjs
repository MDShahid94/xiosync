// ─── Workflow: v0-export-session ──────────────────────────────────────────
// Navigates to v0.app, verifies we are logged in, and exports the full
// storageState (cookies + localStorage) ready for XIOV0 injection.

export const meta = {
  id:          'v0-export-session',
  service:     'v0',
  action:      'export',
  description: 'Verify v0 login and export storageState for XIOV0',
  params: {},
};

export async function run(ctx, _params) {
  await ctx.step('navigate', async () => {
    await ctx.page.goto('https://v0.dev', { waitUntil: 'domcontentloaded', timeout: 15000 });
  });

  await ctx.step('verify_login', async () => {
    const url = ctx.page.url();
    ctx.log(`Current URL: ${url}`);
    if (url.includes('/signin') || url.includes('/login')) {
      throw new Error('Not logged in — run v0-signin first');
    }
  });

  await ctx.step('export_cookies', async () => {
    const storageState = await ctx.page.context().storageState();
    const cookies = storageState.cookies ?? [];
    ctx.log(`Exporting ${cookies.length} cookies and ${storageState.origins?.length ?? 0} origins`);
    ctx.setResult({ storageState, cookie_count: cookies.length });
  });
}
