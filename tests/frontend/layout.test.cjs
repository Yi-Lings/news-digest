// Run with: node --test tests/frontend/layout.test.cjs
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const { runInNewContext } = require('node:vm');

const source = readFileSync(resolve(__dirname, '../../src/news_digest/static/layout.js'), 'utf8');
const edgeDesktop = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0';
const edgeMobile = 'Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/140.0.0.0 Mobile Safari/537.36 EdgA/140.0.0.0';

function reader(options = {}) {
  const attrs = {};
  const listeners = {};
  const storage = { value: options.saved || null };
  const viewport = { content: 'width=device-width, initial-scale=1' };
  const primaryTouch = { matches: Boolean(options.primaryTouch), addEventListener() {} };
  const buttons = ['auto', 'mobile', 'desktop'].map(layout => ({
    dataset: { layoutBtn: layout },
    setAttribute(name, value) { this[name] = value; },
    addEventListener(name, callback) { this[name] = callback; }
  }));
  const root = {
    clientWidth: options.width || 1200,
    style: { setProperty() {} },
    setAttribute(name, value) { attrs[name] = value; }
  };
  const context = {
    document: {
      documentElement: root,
      querySelector(selector) { return selector.startsWith('meta') ? viewport : null; },
      querySelectorAll() { return buttons; },
      addEventListener(name, callback) { listeners[name] = callback; }
    },
    navigator: {
      maxTouchPoints: options.touchPoints || 0,
      userAgent: options.userAgent || 'Mozilla/5.0 (X11; Linux x86_64) Chrome/140.0.0.0',
      userAgentData: { mobile: Boolean(options.mobileHint) }
    },
    window: {
      screen: { width: options.screenWidth || 1200 },
      innerWidth: options.width || 1200,
      innerHeight: 800,
      matchMedia: options.noMedia ? undefined : () => primaryTouch,
      addEventListener() {}
    },
    localStorage: {
      getItem() { if (options.blockStorage) throw Error('blocked'); return storage.value; },
      setItem(key, value) { if (options.blockStorage) throw Error('blocked'); storage.value = value; }
    }
  };
  runInNewContext(source, context);
  listeners.DOMContentLoaded();
  return { attrs, viewport, buttons, storage, context };
}

test('automatic mode follows Edge request-desktop-site despite touch input', () => {
  const page = reader({ width: 980, screenWidth: 390, touchPoints: 5, primaryTouch: true, userAgent: edgeDesktop });
  assert.equal(page.attrs['data-layout'], 'desktop');
  assert.equal(page.attrs['data-layout-preference'], 'auto');
  assert.equal(page.viewport.content, 'width=1200');
  assert.equal(page.buttons[0]['aria-pressed'], 'true');
});

test('desktop request is honored even if the browser reports a narrow viewport', () => {
  assert.equal(reader({ width: 390, screenWidth: 390, touchPoints: 5, userAgent: edgeDesktop }).attrs['data-layout'], 'desktop');
});

test('Android desktop request without the Mobile token also selects desktop', () => {
  const page = reader({ width: 980, screenWidth: 390, primaryTouch: true,
    userAgent: edgeMobile.replace('Mobile Safari', 'Safari') });
  assert.equal(page.attrs['data-layout'], 'desktop');
});

test('normal Edge mobile and landscape touch browsing retain the mobile layout', () => {
  for (const width of [390, 1080]) {
    assert.equal(reader({ width, screenWidth: width, primaryTouch: true, userAgent: edgeMobile, mobileHint: true }).attrs['data-layout'], 'mobile');
  }
});

test('iPad desktop user agent with touch follows the browser desktop request', () => {
  const page = reader({ width: 1024, screenWidth: 1024, primaryTouch: true, touchPoints: 5,
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15' });
  assert.equal(page.attrs['data-layout'], 'desktop');
});

test('touch laptop with a mouse or trackpad keeps the desktop layout', () => {
  assert.equal(reader({ width: 1366, screenWidth: 1366, touchPoints: 10 }).attrs['data-layout'], 'desktop');
  assert.equal(reader({ width: 800, screenWidth: 1366, touchPoints: 10 }).attrs['data-layout'], 'mobile');
});

test('narrow desktop window still uses the responsive mobile layout', () => {
  assert.equal(reader({ width: 800, screenWidth: 1920 }).attrs['data-layout'], 'mobile');
});

test('phone screen and mobile browser hints remain fallbacks', () => {
  for (const options of [
    { width: 980, screenWidth: 390, touchPoints: 5, noMedia: true, userAgent: 'Unknown' },
    { width: 980, screenWidth: 980, mobileHint: true, noMedia: true },
    { width: 980, screenWidth: 980, userAgent: 'Mozilla/5.0 (Linux; Android 16) Chrome/140 Mobile', noMedia: true }
  ]) assert.equal(reader(options).attrs['data-layout'], 'mobile');
});

test('manual desktop wins until auto is selected, and the auto choice survives navigation', () => {
  const options = { width: 980, screenWidth: 980, primaryTouch: true, saved: 'desktop', userAgent: edgeMobile };
  const page = reader(options);
  assert.equal(page.attrs['data-layout'], 'desktop');
  assert.equal(page.viewport.content, 'width=1200');
  page.buttons[0].click();
  assert.equal(page.attrs['data-layout'], 'mobile');
  assert.equal(page.storage.value, 'auto');
  assert.match(page.viewport.content, /^width=device-width/);
  assert.equal(reader({ ...options, saved: page.storage.value }).attrs['data-layout'], 'mobile');
});

test('manual mobile can override a browser desktop request, then auto follows it again', () => {
  const options = { width: 980, screenWidth: 390, primaryTouch: true, userAgent: edgeDesktop, saved: 'mobile' };
  const page = reader(options);
  assert.equal(page.attrs['data-layout'], 'mobile');
  page.buttons[0].click();
  assert.equal(page.attrs['data-layout'], 'desktop');
  assert.equal(page.viewport.content, 'width=1200');
  assert.equal(reader({ ...options, saved: page.storage.value }).attrs['data-layout'], 'desktop');
});

test('automatic mode follows a browser switch back to normal mobile browsing', () => {
  const options = { width: 980, screenWidth: 390, primaryTouch: true, saved: 'auto' };
  assert.equal(reader({ ...options, userAgent: edgeDesktop }).attrs['data-layout'], 'desktop');
  assert.equal(reader({ ...options, userAgent: edgeMobile, mobileHint: true }).attrs['data-layout'], 'mobile');
});

test('manual mobile and auto switching work even when storage is blocked', () => {
  const page = reader({ blockStorage: true });
  page.buttons[1].click();
  assert.equal(page.attrs['data-layout'], 'mobile');
  page.buttons[0].click();
  assert.equal(page.attrs['data-layout'], 'desktop');
});
