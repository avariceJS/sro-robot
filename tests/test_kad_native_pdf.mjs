import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const ROOT = new URL('../browser-extension/kad-helper/', import.meta.url);
const CARD_ID = '11111111-2222-3333-4444-555555555555';
const DOC_ID = '12345678-1234-1234-1234-123456789012';
const PDF_URL = `https://kad.arbitr.ru/Kad/PdfDocument/${CARD_ID}/${DOC_ID}/act.pdf`;
const PDF_REDIRECT = `https://kad.arbitr.ru/Document/Pdf/${CARD_ID}/${DOC_ID}/act.pdf?isAddStamp=True`;
const OTHER_HOST = `https://evil.example/Kad/PdfDocument/${CARD_ID}/${DOC_ID}/act.pdf`;
const bytes = new TextEncoder().encode('%PDF-1.7 synthetic body');

function event() {
  const listeners = new Set();
  return {
    addListener(fn, ...args) { listeners.add(fn); this.lastOptions = args; },
    removeListener(fn) { listeners.delete(fn); },
    fire(details) { for (const fn of [...listeners]) fn(details); },
    get size() { return listeners.size; },
    lastOptions: null,
  };
}

async function harness({available = true, updateFails = false} = {}) {
  const headers = event();
  const redirects = event();
  const errors = event();
  const filters = new Map();
  const updates = [];
  const timers = new Map();
  let nextTimer = 1;
  const webRequest = {
    onHeadersReceived: headers,
    onBeforeRedirect: redirects,
    onErrorOccurred: errors,
    ...(available ? {filterResponseData(id) {
      const writes = [];
      const f = {writes, started: false, disconnected: false, closed: false,
        write(chunk) { writes.push(new Uint8Array(chunk)); },
        disconnect() { if (!this.started) throw new Error('not_started'); this.disconnected = true; },
        close() { this.closed = true; }};
      filters.set(id, f);
      return f;
    }} : {}),
  };
  const api = {webRequest, tabs: {async update(id, options) {
    updates.push({id, ...options});
    if (updateFails) throw new Error('navigation_failed');
  }}};
  const sandbox = {
    URL, Uint8Array, Number, String, Error, Promise, Math, Object, Array,
    setTimeout(fn, delay) { const id = nextTimer++; timers.set(id, {fn, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  sandbox.browser = api;
  const context = vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL('native-pdf.js', ROOT), 'utf8'), context, {filename: 'native-pdf.js'});
  return {capture: context.SroKadNativePdf.capture, headers, redirects, errors, filters, updates, timers};
}

function details(overrides = {}) {
  return {tabId: 7, type: 'main_frame', frameId: 0, requestId: 'r1', url: PDF_URL,
    statusCode: 200, responseHeaders: [{name: 'Content-Type', value: 'application/pdf'}], ...overrides};
}

function stream(h, requestId = 'r1', parts = [bytes]) {
  const filter = h.filters.get(requestId);
  assert.ok(filter);
  filter.started = true;
  filter.onstart();
  for (const part of parts) filter.ondata({data: part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength)});
  filter.onstop();
  return filter;
}

test('captures exact PDF bytes while forwarding each original chunk unchanged', async () => {
  const h = await harness();
  const resultPromise = h.capture({tabId: 7, url: PDF_URL});
  assert.equal(JSON.stringify(h.updates), JSON.stringify([{id: 7, url: PDF_URL}]));
  assert.equal(h.headers.size, 1);
  assert.equal(JSON.stringify(h.headers.lastOptions[0]), JSON.stringify({urls: ['https://kad.arbitr.ru/*'], types: ['main_frame'], tabId: 7}));
  assert.equal(JSON.stringify(h.headers.lastOptions[1]), JSON.stringify(['blocking', 'responseHeaders']));
  h.headers.fire(details());
  const filter = stream(h, 'r1', [bytes.subarray(0, 2), bytes.subarray(2, 7), bytes.subarray(7)]);
  assert.deepEqual([...await resultPromise], [...bytes]);
  assert.deepEqual(filter.writes.flatMap(chunk => [...chunk]), [...bytes]);
  assert.equal(filter.closed, true);
  assert.equal(h.headers.size + h.redirects.size + h.errors.size, 0);
});

test('permits only the validated KAD PDF redirect and its exact stamp query', async () => {
  const h = await harness();
  const resultPromise = h.capture({tabId: 7, url: PDF_URL});
  h.redirects.fire(details({statusCode: 302, redirectUrl: PDF_REDIRECT}));
  h.headers.fire(details({url: PDF_REDIRECT, statusCode: 200}));
  stream(h);
  assert.deepEqual([...await resultPromise], [...bytes]);

  for (const redirectUrl of [OTHER_HOST, PDF_REDIRECT.replace('?isAddStamp=True', '?other=true'),
    PDF_REDIRECT.replace('/Document/Pdf/', '/other/')]) {
    const bad = await harness();
    const pending = bad.capture({tabId: 7, url: PDF_URL});
    bad.redirects.fire(details({statusCode: 302, redirectUrl}));
    await assert.rejects(pending, /pdf_redirect/);
    assert.equal(bad.filters.size, 0);
  }
});

test('rejects invalid selected URLs without navigation or listeners', async () => {
  for (const url of [OTHER_HOST, `${PDF_URL}?x=1`, PDF_URL.replace('/Kad/PdfDocument/', '/other/')]) {
    const h = await harness();
    await assert.rejects(h.capture({tabId: 7, url}), /invalid_pdf_url/);
    assert.equal(h.updates.length, 0);
    assert.equal(h.headers.size, 0);
  }
  const h = await harness({available: false});
  await assert.rejects(h.capture({tabId: 7, url: PDF_URL}), /native_pdf_unavailable/);
});

test('ignores other tabs, subframes, unrelated URLs and unrelated request errors', async () => {
  const h = await harness();
  const pending = h.capture({tabId: 7, url: PDF_URL});
  h.headers.fire(details({tabId: 8}));
  h.headers.fire(details({frameId: 1}));
  h.headers.fire(details({type: 'xmlhttprequest'}));
  h.headers.fire(details({url: OTHER_HOST}));
  assert.equal(h.filters.size, 0);
  h.headers.fire(details());
  h.errors.fire(details({requestId: 'other'}));
  assert.equal(h.filters.size, 1);
  stream(h);
  assert.deepEqual([...await pending], [...bytes]);
});

test('stops explicitly on HTML, authentication challenge, rate limit and other HTTP failures', async () => {
  for (const [overrides, reason] of [
    [{responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}, 'pdf_html'],
    [{statusCode: 401}, 'challenge'], [{statusCode: 403}, 'challenge'], [{statusCode: 429}, 'rate-limited'],
    [{statusCode: 500}, 'pdf_http'],
  ]) {
    const h = await harness();
    const pending = h.capture({tabId: 7, url: PDF_URL});
    h.headers.fire(details(overrides));
    await assert.rejects(pending, new RegExp(reason));
    assert.equal(h.filters.size, 0);
    assert.equal(h.headers.size, 0);
  }
});

test('rejects oversized data and invalid PDF magic, disconnecting the filter', async () => {
  const large = await harness();
  const tooBig = large.capture({tabId: 7, url: PDF_URL});
  large.headers.fire(details());
  const oversized = new Uint8Array(25 * 1024 * 1024 + 1);
  const filter = large.filters.get('r1');
  filter.started = true;
  filter.onstart();
  filter.ondata({data: oversized.buffer});
  await assert.rejects(tooBig, /pdf_too_large/);
  assert.equal(filter.disconnected, true);

  const invalid = await harness();
  const notPdf = invalid.capture({tabId: 7, url: PDF_URL});
  invalid.headers.fire(details());
  const badFilter = stream(invalid, 'r1', [new TextEncoder().encode('<html>')]);
  await assert.rejects(notPdf, /not_pdf/);
  assert.equal(badFilter.disconnected, true);
});

test('rejects duplicate request IDs and network errors for the owned request', async () => {
  const duplicate = await harness();
  const pendingDuplicate = duplicate.capture({tabId: 7, url: PDF_URL});
  duplicate.headers.fire(details());
  const duplicateFilter = duplicate.filters.get('r1');
  duplicateFilter.started = true;
  duplicateFilter.onstart();
  duplicate.headers.fire(details({requestId: 'r2'}));
  await assert.rejects(pendingDuplicate, /pdf_duplicate_request/);
  assert.equal(duplicate.filters.get('r1').disconnected, true);

  const network = await harness();
  const pendingNetwork = network.capture({tabId: 7, url: PDF_URL});
  network.errors.fire(details());
  await assert.rejects(pendingNetwork, /pdf_network/);
  const beforeHeaders = await harness();
  const pendingBeforeHeaders = beforeHeaders.capture({tabId: 7, url: PDF_URL});
  beforeHeaders.errors.fire(details());
  await assert.rejects(pendingBeforeHeaders, /pdf_network/);
});

test('cancels and times out with listeners, timer and captured chunks released', async () => {
  const cancelled = await harness();
  const controller = new AbortController();
  const pendingCancel = cancelled.capture({tabId: 7, url: PDF_URL, signal: controller.signal});
  cancelled.headers.fire(details());
  const cancelFilter = cancelled.filters.get('r1');
  cancelFilter.started = true;
  cancelFilter.onstart();
  cancelled.filters.get('r1').ondata({data: bytes.buffer.slice(0, 2)});
  controller.abort();
  await assert.rejects(pendingCancel, /aborted/);
  assert.equal(cancelled.filters.get('r1').disconnected, true);
  assert.equal(cancelled.headers.size + cancelled.redirects.size + cancelled.errors.size, 0);
  assert.equal(cancelled.timers.size, 0);

  const beforeStart = await harness();
  const pendingBeforeStart = beforeStart.capture({tabId: 7, url: PDF_URL});
  beforeStart.headers.fire(details());
  const earlyFilter = beforeStart.filters.get('r1');
  beforeStart.errors.fire(details());
  await assert.rejects(pendingBeforeStart, /pdf_network/);
  assert.equal(earlyFilter.disconnected, false);
  earlyFilter.started = true;
  earlyFilter.onstart();
  assert.equal(earlyFilter.disconnected, true);

  const timed = await harness();
  const pendingTimeout = timed.capture({tabId: 7, url: PDF_URL});
  const timer = [...timed.timers.values()][0];
  assert.equal(timer.delay, 60000);
  timer.fn();
  await assert.rejects(pendingTimeout, /pdf_timeout/);
  assert.equal(timed.headers.size + timed.redirects.size + timed.errors.size, 0);
  assert.equal(timed.timers.size, 0);
});

test('aborted signal and tab navigation failure reject cleanly', async () => {
  const h = await harness();
  const controller = new AbortController(); controller.abort();
  await assert.rejects(h.capture({tabId: 7, url: PDF_URL, signal: controller.signal}), /aborted/);
  assert.equal(h.headers.size, 0);

  const failing = await harness({updateFails: true});
  await assert.rejects(failing.capture({tabId: 7, url: PDF_URL}), /pdf_navigation/);
  assert.equal(failing.headers.size + failing.redirects.size + failing.errors.size, 0);
});
