// ─── Workflow: v0-signin ─────────────────────────────────────────────────────
// Signs into v0.app using email + password with human interaction simulation.
// Uses humanType (char-by-char) and humanClick (stepped mouse) from human.mjs.

import { humanType, humanClick, humanSleep, firstVisible, waitForVisible } from '../src/utils/human.mjs';

export const meta = {
  id:          'v0-signin',
  service:     'v0',
  action:      'signin',
  description: 'Sign into v0.app with email and password — human typing simulation, no instant fill()',
  params: {
    email:    { type: 'string', required: true,  description: 'V0 account email' },
    password: { type: 'string', required: true,  description: 'V0 account password' },
  },
};

export async function run(ctx, params) {
  const { email, password } = params;

  await ctx.step('navigate', async () => {
    await ctx.page.goto('https://v0.dev', { waitUntil: 'domcontentloaded' });
    await humanSleep(800, 1500);
  });

  await ctx.step('open_signin', async () => {
    // Click Sign In button if present on homepage
    const signinSel = await firstVisible(ctx.page, [
      'a[href*="/signin"]',
      'button:has-text("Sign in")',
      'a:has-text("Sign in")',
    ], 5000);

    if (signinSel) {
      await humanClick(ctx.page, signinSel);
      await ctx.page.waitForNavigation({ waitUntil: 'domcontentloaded' }).catch(() => {});
    } else {
      await ctx.page.goto('https://v0.dev/signin', { waitUntil: 'domcontentloaded' });
    }
    await humanSleep(800, 1800);
  });

  await ctx.step('enter_email', async () => {
    await waitForVisible(ctx.page, 'input[type="email"]', 12000);
    await humanType(ctx.page, 'input[type="email"]', email);
    await humanSleep(300, 700);

    const submitSel = await firstVisible(ctx.page, [
      'button[type="submit"]',
      'button:has-text("Continue")',
      'button:has-text("Next")',
    ], 4000);
    if (submitSel) {
      await humanClick(ctx.page, submitSel);
    } else {
      await ctx.page.keyboard.press('Enter');
    }
    await humanSleep(1200, 2500);
  });

  await ctx.step('enter_password', async () => {
    await waitForVisible(ctx.page, 'input[type="password"]', 12000);
    await humanType(ctx.page, 'input[type="password"]', password);
    await humanSleep(400, 900);

    const submitSel = await firstVisible(ctx.page, [
      'button[type="submit"]',
      'button:has-text("Sign in")',
      'button:has-text("Continue")',
    ], 4000);
    if (submitSel) {
      await humanClick(ctx.page, submitSel);
    } else {
      await ctx.page.keyboard.press('Enter');
    }
    await humanSleep(2000, 4000);
  });

  await ctx.step('verify', async () => {
    const url = ctx.page.url();
    ctx.log(`Final URL: ${url}`);

    const isLoggedIn = !url.includes('/signin') && !url.includes('/login');
    if (!isLoggedIn) {
      throw new Error(`Still on auth page after login: ${url}`);
    }

    const cookies = await ctx.page.context().cookies();
    ctx.log(`Captured ${cookies.length} cookies`);

    ctx.setResult({
      success:      true,
      final_url:    url,
      cookie_count: cookies.length,
    });
  });
}
