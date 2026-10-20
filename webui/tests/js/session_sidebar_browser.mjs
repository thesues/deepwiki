// Run with Playwright installed: NODE_PATH=<node_modules> node this-file.mjs.
// Uses the shipped HTML/CSS/JS; API responses are stubbed to isolate navigation.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
const { chromium } = createRequire(import.meta.url)('playwright');
const root = fileURLToPath(new URL('../../static/', import.meta.url));
const browser = await chromium.launch({ headless: true });
const endpoint = { key: 'test', label: 'Test', model: 'test', maxConcurrent: 1, running: 0 };
try {
  for (const [width, blockedStorage] of [[390, false], [390, true], [820, false], [821, false]]) {
    const mobile = width <= 820;
    const context = await browser.newContext({ viewport: { width, height: 844 }, hasTouch: mobile, isMobile: mobile });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    if (blockedStorage) await page.addInitScript(() => {
      for (const method of ['getItem', 'setItem']) Storage.prototype[method] = () => { throw new Error('storage disabled'); };
    });
    let release;
    const sessionsGate = new Promise(resolve => { release = resolve; });
    await page.route('http://webui.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) {
        let body = {};
        if (url.pathname === '/api/sessions') {
          await sessionsGate;
          body = { sessions: [{ id: 'old-session', title: '旧会话', messageCount: 0 }], streaming: {}, endpoints: [endpoint] };
        } else if (url.pathname === '/api/status') body = { endpoints: [endpoint], defaultEndpoint: 'test' };
        else if (url.pathname === '/api/session/stream') return route.fulfill({ contentType: 'text/event-stream', body: ': connected\n\n' });
        else body = { messages: [], hasMoreBefore: false, hasMoreAfter: false };
        return route.fulfill({ json: body });
      }
      const relative = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/static\//, '');
      try {
        const body = process.env.BASELINE && ['index.html', 'app.js', 'style.css'].includes(relative)
          ? execFileSync('git', ['show', `HEAD:webui/static/${relative}`])
          : await fs.readFile(path.join(root, relative));
        const contentType = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' }[path.extname(relative)] || 'application/octet-stream';
        return route.fulfill({ body, contentType });
      } catch { return route.fulfill({ status: 404 }); }
    });
    await page.goto('http://webui.test/', { waitUntil: 'domcontentloaded' });
    const toggle = page.locator('#sessions-toggle');
    const sidebar = page.locator('.sessions');
    const chat = page.locator('.chat');
    const click = locator => mobile ? locator.tap() : locator.click();
    const check = async expanded => {
      assert.equal(await toggle.isVisible(), true, 'toggle must be visible at every width');
      assert.equal(await toggle.getAttribute('aria-expanded'), String(expanded));
      assert.equal(await sidebar.isVisible(), expanded);
      assert.equal(await chat.isVisible(), !mobile || !expanded);
    };
    await check(!mobile);
    // Taps work even while the first session request is still pending.
    await click(toggle); await check(mobile);
    await click(toggle); await check(!mobile);
    release();
    await page.waitForFunction(() => document.querySelectorAll('#sessions li').length === 1);
    if (mobile) {
      await click(toggle); await check(true);
      await click(page.locator('#sessions li .t')); await check(false);
      await click(toggle); await check(true);
      await click(page.locator('#new-session')); await check(false);
      // Rotate to desktop and back: the same state and control remain usable.
      await page.setViewportSize({ width: 1024, height: 768 });
      await click(toggle);
      assert.equal(await sidebar.isVisible(), true);
      assert.equal(await chat.isVisible(), true);
      await page.setViewportSize({ width, height: 844 });
      await check(true); await click(toggle); await check(false);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    }
    if (!blockedStorage) {
      await click(toggle);
      const expanded = await toggle.getAttribute('aria-expanded');
      await page.reload({ waitUntil: 'domcontentloaded' });
      await check(expanded === 'true');
    }
    assert.deepEqual(errors, []);
    console.log(`ok - sidebar ${width}px, storage ${blockedStorage ? 'blocked' : 'available'}`);
    await context.close();
  }
} finally { await browser.close(); }
