// ─── Human Interaction Utilities ─────────────────────────────────────────────
// Sourced from XIO_VERSE 2/_SYSTEM/Core/auth_manager.py (human_type, human_click)
// and Colab_Antibot_Browser.ipynb (mouse movement patterns).
//
// These replace page.fill() and page.click() in all auth workflows.
// Detectable patterns eliminated:
//   ✗ page.fill()      → sets value instantly (triggers input event only)
//   ✗ page.click()     → zero-duration click at exact center
//   ✓ humanType()      → char-by-char with random inter-key delays
//   ✓ humanClick()     → mouse move with steps + random offset + settle pause

/**
 * Random integer between min and max (inclusive).
 */
export function randInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

/**
 * Random float between min and max.
 */
export function randFloat(min, max) {
  return Math.random() * (max - min) + min;
}

/**
 * Sleep for a random number of ms between min and max.
 * Use between steps to simulate human think time.
 */
export async function humanSleep(minMs = 300, maxMs = 900) {
  const ms = randInt(minMs, maxMs);
  await new Promise(r => setTimeout(r, ms));
}

/**
 * Type text into a page element character-by-character with realistic timing.
 * Sources: auth_manager.py human_type() + Colab_Antibot_Browser.ipynb typing simulation.
 *
 * @param {import('playwright-core').Page} page
 * @param {string} selector  - CSS/XPath selector or Playwright locator string
 * @param {string} text      - Text to type
 * @param {object} opts
 * @param {number} opts.minDelay  - Min ms between keystrokes (default 45)
 * @param {number} opts.maxDelay  - Max ms between keystrokes (default 140)
 * @param {boolean} opts.clearFirst - Select-all + backspace before typing (default true)
 */
export async function humanType(page, selector, text, opts = {}) {
  const {
    minDelay  = 45,
    maxDelay  = 140,
    clearFirst = true,
  } = opts;

  const locator = page.locator(selector).first();
  await locator.waitFor({ state: 'visible', timeout: 10000 });

  // Click to focus before typing
  await humanClick(page, selector);
  await humanSleep(80, 200);

  if (clearFirst) {
    // Select all + delete (handles pre-filled values)
    await page.keyboard.press('Control+a');
    await humanSleep(50, 120);
    await page.keyboard.press('Backspace');
    await humanSleep(60, 150);
  }

  // Type each character with a random delay
  for (const ch of text) {
    await page.keyboard.type(ch, { delay: 0 });
    const delay = randInt(minDelay, maxDelay);
    // Occasional "thinking pause" (simulates natural hesitation ~8% of chars)
    const pause = Math.random() < 0.08 ? randInt(200, 600) : delay;
    await new Promise(r => setTimeout(r, pause));
  }
}

/**
 * Click an element with human-like mouse movement.
 * Moves in small steps toward the element center + random offset,
 * then dispatches the click. Avoids exact-center pixel precision.
 * Sources: auth_manager.py human_click() pattern.
 *
 * @param {import('playwright-core').Page} page
 * @param {string} selector
 * @param {object} opts
 * @param {number} opts.steps        - Mouse move steps (default 8)
 * @param {number} opts.offsetRange  - Max random offset from center in px (default 5)
 * @param {number} opts.settleMs     - Pause after click in ms (default 80-250)
 */
export async function humanClick(page, selector, opts = {}) {
  const {
    steps       = randInt(6, 12),
    offsetRange = 5,
    button      = 'left',
  } = opts;

  const locator = page.locator(selector).first();
  await locator.waitFor({ state: 'visible', timeout: 10000 });

  const box = await locator.boundingBox();
  if (!box) {
    // Fallback to direct click if element is not in viewport
    await locator.click({ button });
    return;
  }

  // Random offset from center (never click dead center)
  const cx = box.x + box.width  / 2 + randFloat(-offsetRange, offsetRange);
  const cy = box.y + box.height / 2 + randFloat(-offsetRange, offsetRange);

  await page.mouse.move(cx, cy, { steps });
  await humanSleep(40, 120);
  await page.mouse.click(cx, cy, { button });
  await humanSleep(60, 200);
}

/**
 * Scroll the page gradually to simulate human reading.
 * @param {import('playwright-core').Page} page
 * @param {number} pixels - Total pixels to scroll (positive = down)
 * @param {number} steps  - Number of incremental scrolls
 */
export async function humanScroll(page, pixels = 400, steps = 5) {
  const increment = pixels / steps;
  for (let i = 0; i < steps; i++) {
    await page.mouse.wheel(0, increment);
    await humanSleep(80, 200);
  }
}

/**
 * Warm up browser history to appear like a real user before auth.
 * Sourced from auth_manager.py smart_login() Phase 1.
 * Visits google.com + wikipedia, scrolls, pauses — builds trust signals.
 *
 * @param {import('playwright-core').Page} page
 */
export async function warmupHistory(page) {
  try {
    await page.goto('https://www.google.com', { waitUntil: 'domcontentloaded', timeout: 15000 });
    await humanSleep(800, 1800);
    await humanScroll(page, randInt(100, 300), 3);
    await humanSleep(500, 1200);

    await page.goto('https://en.wikipedia.org/wiki/Main_Page', { waitUntil: 'domcontentloaded', timeout: 15000 });
    await humanSleep(1000, 2500);
    await humanScroll(page, randInt(300, 700), 5);
    await humanSleep(600, 1400);
  } catch (_) {
    // Warmup is best-effort — don't fail the workflow if it errors
  }
}

/**
 * Check if Google account is already authenticated in this context.
 * Returns true if myaccount.google.com loads without a sign-in prompt.
 * Sourced from auth_manager.py smart_login() session pre-check.
 *
 * @param {import('playwright-core').Page} page
 * @returns {Promise<boolean>}
 */
export async function isGoogleAuthenticated(page) {
  try {
    await page.goto('https://myaccount.google.com/', {
      waitUntil: 'domcontentloaded',
      timeout: 12000,
    });
    await humanSleep(1000, 2000);
    const url = page.url();
    const html = await page.content();
    const isOnAccount = url.includes('myaccount.google.com');
    const hasSignInPrompt = html.toLowerCase().includes('sign in') ||
                            html.toLowerCase().includes('create an account');
    return isOnAccount && !hasSignInPrompt;
  } catch (_) {
    return false;
  }
}

/**
 * Wait for a selector to appear with a human-plausible timeout.
 * Throws if not found within timeout.
 */
export async function waitForVisible(page, selector, timeoutMs = 12000) {
  await page.waitForSelector(selector, { state: 'visible', timeout: timeoutMs });
}

/**
 * Try multiple selectors, return the first that is visible.
 * Useful for Google's A/B tested UI variants.
 */
export async function firstVisible(page, selectors, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const sel of selectors) {
      try {
        const el = page.locator(sel).first();
        if (await el.isVisible({ timeout: 200 })) return sel;
      } catch (_) {}
    }
    await humanSleep(150, 300);
  }
  return null;
}
