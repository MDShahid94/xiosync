import { createLogger } from '../utils/logger.mjs';

const log = createLogger('session-verifier');

/**
 * Robustly verify if a Google account is logged into the given page.
 * Navigates to myaccount.google.com and checks the visible email address.
 * 
 * @param {object} page - Playwright/patchright page object
 * @param {string} expectedEmail - The email prefix expected to be logged in (e.g. 'user' for 'user@gmail.com')
 * @returns {Promise<boolean>} true if the session is active and matches the email
 */
export async function verifyGoogleSession(page, expectedEmail) {
  log.info(`Verifying Google session for expected email prefix: ${expectedEmail.split('@')[0]}`);
  
  await page.goto('https://myaccount.google.com/', {
    waitUntil: 'domcontentloaded', 
    timeout: 15000,
  }).catch(() => {});
  
  // Wait a moment for redirects (e.g. to sign-in page if expired)
  await new Promise(r => setTimeout(r, 1500));
  
  const url = page.url();
  if (url.includes('myaccount.google.com') && !url.includes('signin')) {
    // The page is an SPA, so wait up to 15s for the text to appear in the body
    const emailPrefix = expectedEmail.split('@')[0].toLowerCase();
    const isMatched = await page.waitForFunction((prefix) => {
      return document.body && document.body.innerText.toLowerCase().includes(prefix);
    }, emailPrefix, { timeout: 15000 }).catch(() => false);
    
    if (isMatched) {
      log.info(`✅ Session valid for ${expectedEmail}`);
      return true;
    } else {
      log.warn(`⚠️ Session active but for a DIFFERENT account! Expected: ${emailPrefix}`);
      return false;
    }
  } else {
    log.warn(`⚠️ Session expired or invalid (Redirected to: ${url})`);
    return false;
  }
}
