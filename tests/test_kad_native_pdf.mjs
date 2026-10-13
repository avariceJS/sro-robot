import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const ROOT = new URL('../browser-extension/kad-helper/', import.meta.url);
const CARD_ID = '11111111-2222-3333-4444-555555555555';
const DOC_ID = '12345678-1234-1234-1234-123456789012';
const PDF_URL = `https://kad.arbitr.ru/Kad/PdfDocument/${CARD_ID}/${DOC_ID}/act.pdf`;
const PDF_REDIRECT = `https://kad.arbitr.ru/Document/Pdf/${CARD_ID}/${DOC_ID}/act.pdf?isAddStamp=True`;
const OTHER_URL = `https://other.example/Kad/PdfDocument/${CARD_ID}/${DOC_ID}/act.pdf`;
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

async function harness({available = true, updateFails = false, filterFails = false} = {}) {
  const before = event();
  const headers = event();
  const redirects = event();
  const errors = event();
  const filters = new Map();
  const updates = [];
  const timers = new Map();
  let nextTimer = 1;
  const webRequest = {
    onBeforeRequest: before, onHeadersReceived: headers,
    onBeforeRedirect: redirects, onErrorOccurred: errors,
    ...(available ? {filterResponseData(id) {
      if (filterFails) throw new Error('filter_failed');
      const writes = [];
      const f = {writes, started: false, disconnected: false, closed: false,
        write(chunk) { writes.push(new Uint8Array(chunk)); },
        disconnect() { if (!this.started) throw new Error('not_started'); this.disconnected = true; },
        close() { this.closed = true; }};
      const list = filters.get(id) || [];
      list.push(f);
      filters.set(id, list);
      return f;
    }} : {}),
  };
  const api = {webRequest, tabs: {async update(id, options) {
    updates.push({id, ...options});
    if (updateFails) throw new Error('navigation_failed');
  }}};
  const sandbox = {
    TextDecoder, URL, Uint8Array, Number, String, Error, Promise, Math, Object, Array,
    setTimeout(fn, delay) { const id = nextTimer++; timers.set(id, {fn, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  sandbox.browser = api;
  const context = vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL('native-pdf.js', ROOT), 'utf8'), context, {filename: 'native-pdf.js'});
  return {capture: context.SroKadNativePdf.capture, before, headers, redirects, errors, filters, updates, timers};
}

function details(overrides = {}) {
  return {tabId: 7, type: 'main_frame', frameId: 0, method: 'GET', requestId: 'r1', url: PDF_URL,
    statusCode: 200, responseHeaders: [{name: 'Content-Type', value: 'application/pdf'}], ...overrides};
}

function begin(h, overrides = {}) { h.before.fire(details(overrides)); }

function stream(h, requestId = 'r1', parts = [bytes], {stop = true} = {}) {
  const list = h.filters.get(requestId);
  const filter = list?.[list.length - 1];
  assert.ok(filter, `filter exists for ${requestId}`);
  filter.started = true;
  filter.onstart();
  for (const part of parts) filter.ondata({data: part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength)});
  if (stop) filter.onstop();
  return filter;
}

function beginFinalResponse(h) {
  begin(h);
  h.headers.fire(details({statusCode: 302}));
  const redirectFilter = h.filters.get('r1')[0];
  redirectFilter.started = true;
  redirectFilter.onstart();
  redirectFilter.onstop();
  h.redirects.fire(details({statusCode: 302, redirectUrl: PDF_REDIRECT}));
  begin(h, {url: PDF_REDIRECT});
}

function beginHtmlHandoff(h) {
  beginFinalResponse(h);
  h.headers.fire(details({url: PDF_REDIRECT,
    responseHeaders: [{name: 'Content-Type', value: 'text/html; charset=utf-8'}]}));
  return h.filters.get('r1').at(-1);
}

function beginPost(h, requestId = 'post-1', overrides = {}) {
  begin(h, {url: PDF_REDIRECT, method: 'POST', requestId});
  h.headers.fire(details({url: PDF_REDIRECT, method: 'POST', requestId,
    responseHeaders: [{name: 'Content-Type', value: 'application/pdf'}], ...overrides}));
}

test('captures a direct PDF GET and forwards original chunks unchanged', async () => {
  const h = await harness();
  const pending = h.capture({tabId: 7, url: PDF_URL});
  assert.equal(JSON.stringify(h.before.lastOptions[0]), JSON.stringify({urls: ['https://kad.arbitr.ru/*'], types: ['main_frame'], tabId: 7}));
  begin(h);
  h.headers.fire(details());
  const filter = stream(h, 'r1', [bytes.subarray(0, 3), bytes.subarray(3)]);
  assert.deepEqual([...await pending], [...bytes]);
  assert.deepEqual(filter.writes.flatMap(chunk => [...chunk]), [...bytes]);
  assert.equal(filter.closed, true);
  assert.equal(h.before.size + h.headers.size + h.redirects.size + h.errors.size, 0);
});

test('follows only the selected document redirect and passes the final PDF through', async () => {
  const h = await harness();
  const pending = h.capture({tabId: 7, url: PDF_URL});
  beginFinalResponse(h);
  h.headers.fire(details({url: PDF_REDIRECT}));
  const filter = stream(h);
  assert.deepEqual([...await pending], [...bytes]);
  assert.deepEqual(filter.writes.flatMap(chunk => [...chunk]), [...bytes]);

  for (const redirectUrl of [
    'https://evil.example/Document/Pdf/file.pdf?isAddStamp=True',
    PDF_REDIRECT.replace('?isAddStamp=True', '?other=true'),
    PDF_REDIRECT.replace('/Document/Pdf/', '/other/'),
  ]) {
    const bad = await harness();
    const result = bad.capture({tabId: 7, url: PDF_URL});
    begin(bad);
    bad.headers.fire(details({statusCode: 302}));
    const redirectFilter = bad.filters.get('r1')[0];
    redirectFilter.started = true; redirectFilter.onstart(); redirectFilter.onstop();
    bad.redirects.fire(details({statusCode: 302, redirectUrl}));
    await assert.rejects(result, /pdf_redirect/);
    assert.equal(redirectFilter.disconnected, true);
  }
});

test('passively accepts exact browser POST before GET stream stop, and waits for both streams', async () => {
  const h = await harness();
  const pending = h.capture({tabId: 7, url: PDF_URL});
  let didResolve = false;
  pending.then(() => { didResolve = true; }, () => {});
  const getFilter = beginHtmlHandoff(h);
  assert.ok(getFilter);
  const html = new TextEncoder().encode('<html><body>ordinary handoff shell</body></html>');
  const getStarted = stream(h, 'r1', [html.subarray(0, 12), html.subarray(12)], {stop: false});
  beginPost(h, 'browser-post');
  const postFilter = stream(h, 'browser-post', [bytes.subarray(0, 4), bytes.subarray(4)]);
  assert.equal(postFilter.closed, true, 'terminal PDF POST filter closes while awaiting GET terminal');
  await Promise.resolve();
  assert.equal(didResolve, false, 'POST completion alone cannot resolve while GET is not terminal');
  getStarted.onstop();
  assert.deepEqual([...await pending], [...bytes]);
  assert.deepEqual(getStarted.writes.flatMap(chunk => [...chunk]), [...html]);
  assert.deepEqual(postFilter.writes.flatMap(chunk => [...chunk]), [...bytes]);
  assert.equal(getStarted.closed, true);
  assert.equal(postFilter.closed, true);
});

test('accepts POST after a normally completed HTML GET without parsing or inspecting form contents', async () => {
  const h = await harness();
  const pending = h.capture({tabId: 7, url: PDF_URL});
  const getFilter = beginHtmlHandoff(h);
  const html = new TextEncoder().encode('<form><input value="SYNTHETIC-SECRET"></form>');
  stream(h, 'r1', [html]);
  assert.equal(getFilter.closed, true, 'terminal handoff GET filter is closed before awaiting POST');
  beginPost(h, 'post-after-stop');
  stream(h, 'post-after-stop');
  assert.deepEqual([...await pending], [...bytes]);
  assert.deepEqual(getFilter.writes.flatMap(chunk => [...chunk]), [...html]);
});

test('allows only NS_BINDING_ABORTED for the owned GET after the exact POST is accepted', async () => {
  const h = await harness();
  const pending = h.capture({tabId: 7, url: PDF_URL});
  const getFilter = beginHtmlHandoff(h);
  const html = new TextEncoder().encode('<html>synthetic handoff</html>');
  stream(h, 'r1', [html], {stop: false});
  beginPost(h, 'accepted-post');
  h.errors.fire(details({url: PDF_REDIRECT, requestId: 'r1', error: 'NS_BINDING_ABORTED'}));
  const postFilter = stream(h, 'accepted-post');
  assert.deepEqual([...await pending], [...bytes]);
  assert.deepEqual(getFilter.writes.flatMap(chunk => [...chunk]), [...html]);
  assert.equal(getFilter.disconnected, true);
  assert.equal(postFilter.closed, true);

  const unrelatedAbort = await harness();
  const failed = unrelatedAbort.capture({tabId: 7, url: PDF_URL});
  beginHtmlHandoff(unrelatedAbort);
  unrelatedAbort.errors.fire(details({url: PDF_REDIRECT, requestId: 'r1', error: 'NS_BINDING_ABORTED'}));
  await assert.rejects(failed, /pdf_network/);
});

test('scans every HTML chunk including markers split across boundaries and keeps denial statuses explicit', async () => {
  for (const marker of ['CAPTCHA', 'ddos-guard', 'challenge-platform', '__js_p_', 'access denied', 'request blocked']) {
    const h = await harness();
    const pending = h.capture({tabId: 7, url: PDF_URL});
    beginHtmlHandoff(h);
    const value = new TextEncoder().encode(`<html>synthetic ${marker} response</html>`);
    const split = new TextEncoder().encode('<html>synthetic ').length + Math.floor(marker.length / 2);
    const filter = stream(h, 'r1', [value.subarray(0, split), value.subarray(split)]);
    await assert.rejects(pending, /pdf_html_challenge/);
    assert.equal(filter.disconnected, true);
    assert.deepEqual(filter.writes.flatMap(chunk => [...chunk]), [...value]);
  }
  for (const status of [401, 403, 429]) {
    const h = await harness();
    const pending = h.capture({tabId: 7, url: PDF_URL});
    beginFinalResponse(h);
    h.headers.fire(details({url: PDF_REDIRECT, statusCode: status}));
    await assert.rejects(pending, new RegExp(status === 429 ? 'rate-limited' : 'challenge'));
  }
});

test('unknown or false HTML handoff remains pending and times out without replay', async () => {
  for (const body of ['<html>ordinary incomplete document</html>', '<form method="get">synthetic</form>']) {
    const h = await harness();
    const pending = h.capture({tabId: 7, url: PDF_URL});
    beginHtmlHandoff(h);
    const html = new TextEncoder().encode(body);
    stream(h, 'r1', [html]);
    assert.equal(h.updates.length, 1, 'only initial ordinary navigation is issued');
    assert.equal(h.filters.size, 1, 'extension does not synthesize a second request');
    [...h.timers.values()][0].fn();
    await assert.rejects(pending, /pdf_timeout/);
  }
});

test('rejects HTML over 128 KiB and keeps the full GET stream unchanged below that bound', async () => {
  const okay = await harness();
  const pendingOkay = okay.capture({tabId: 7, url: PDF_URL});
  beginHtmlHandoff(okay);
  const body = new Uint8Array(128 * 1024);
  body.fill(65);
  const filter = stream(okay, 'r1', [body]);
  assert.deepEqual(filter.writes.flatMap(chunk => [...chunk]), [...body]);
  [...okay.timers.values()][0].fn();
  await assert.rejects(pendingOkay, /pdf_timeout/);

  const large = await harness();
  const pendingLarge = large.capture({tabId: 7, url: PDF_URL});
  beginHtmlHandoff(large);
  const oversized = new Uint8Array(128 * 1024 + 1);
  const largeFilter = large.filters.get('r1').at(-1);
  largeFilter.started = true; largeFilter.onstart();
  largeFilter.ondata({data: oversized.buffer});
  await assert.rejects(pendingLarge, /pdf_handoff_too_large/);
  assert.equal(largeFilter.disconnected, true);
});

test('rejects duplicate, wrong-method, wrong-URL, and extra tab navigation during handoff', async () => {
  const h = await harness();
  const pending = h.capture({tabId: 7, url: PDF_URL});
  beginHtmlHandoff(h);
  stream(h, 'r1', [new TextEncoder().encode('ordinary html')]);
  begin(h, {url: PDF_REDIRECT, method: 'GET', requestId: 'extra-get'});
  await assert.rejects(pending, /pdf_handoff_mismatch/);

  const sameId = await harness();
  const sameIdResult = sameId.capture({tabId: 7, url: PDF_URL});
  beginHtmlHandoff(sameId);
  stream(sameId, 'r1', [new TextEncoder().encode('ordinary html')]);
  begin(sameId, {url: PDF_REDIRECT, method: 'POST', requestId: 'r1'});
  await assert.rejects(sameIdResult, /pdf_handoff_mismatch/);

  for (const override of [
    {url: PDF_REDIRECT, method: 'POST', requestId: 'post-1'},
  ]) {
    const duplicate = await harness();
    const result = duplicate.capture({tabId: 7, url: PDF_URL});
    beginHtmlHandoff(duplicate);
    stream(duplicate, 'r1', [new TextEncoder().encode('ordinary html')]);
    begin(duplicate, override);
    begin(duplicate, {...override, requestId: 'post-2'});
    await assert.rejects(result, /pdf_handoff_mismatch/);
  }

  const firstNavigation = await harness();
  const firstResult = firstNavigation.capture({tabId: 7, url: PDF_URL});
  begin(firstNavigation, {url: PDF_REDIRECT.replace('act.pdf', 'other.pdf')});
  await assert.rejects(firstResult, /pdf_navigation_mismatch/);
});

test('requires status 200, PDF MIME and PDF magic on POST; rejects POST redirects', async () => {
  for (const [statusCode, mime, body, expected] of [
    [206, 'application/pdf', bytes, 'pdf_http'],
    [200, 'text/html', bytes, 'pdf_mime'],
    [200, 'application/pdf', new TextEncoder().encode('not a PDF'), 'not_pdf'],
    [302, 'text/html', bytes, 'pdf_post_redirect'],
  ]) {
    const h = await harness();
    const pending = h.capture({tabId: 7, url: PDF_URL});
    beginHtmlHandoff(h);
    stream(h, 'r1', [new TextEncoder().encode('ordinary html')]);
    beginPost(h, 'post-1', {statusCode, responseHeaders: [{name: 'Content-Type', value: mime}]});
    if (statusCode === 302) await assert.rejects(pending, new RegExp(expected));
    else {
      stream(h, 'post-1', [body]);
      await assert.rejects(pending, new RegExp(expected));
    }
  }
});

test('rejects invalid selected URLs, unavailable capture, network errors and oversize PDFs', async () => {
  for (const url of [OTHER_URL, `${PDF_URL}?x=1`, PDF_URL.replace('/Kad/PdfDocument/', '/other/')]) {
    const h = await harness();
    await assert.rejects(h.capture({tabId: 7, url}), /invalid_pdf_url/);
    assert.equal(h.updates.length, 0);
    assert.equal(h.headers.size, 0);
  }
  const unavailable = await harness({available: false});
  await assert.rejects(unavailable.capture({tabId: 7, url: PDF_URL}), /native_pdf_unavailable/);

  const network = await harness();
  const networkPending = network.capture({tabId: 7, url: PDF_URL});
  begin(network);
  network.errors.fire(details({error: 'NS_ERROR_NET_RESET'}));
  await assert.rejects(networkPending, /pdf_network/);

  const large = await harness();
  const tooLarge = large.capture({tabId: 7, url: PDF_URL});
  begin(large);
  large.headers.fire(details());
  const filter = large.filters.get('r1')[0];
  filter.started = true; filter.onstart();
  filter.ondata({data: new Uint8Array(25 * 1024 * 1024 + 1).buffer});
  await assert.rejects(tooLarge, /pdf_too_large/);
  assert.equal(filter.disconnected, true);
});

test('abort and shared timeout clean up both accepted filters', async () => {
  const h = await harness();
  const controller = new AbortController();
  const pending = h.capture({tabId: 7, url: PDF_URL, signal: controller.signal});
  beginHtmlHandoff(h);
  const getFilter = stream(h, 'r1', [new TextEncoder().encode('ordinary html')], {stop: false});
  beginPost(h, 'post-1');
  const postFilter = h.filters.get('post-1')[0];
  controller.abort();
  await assert.rejects(pending, /aborted/);
  assert.equal(getFilter.disconnected, true);
  postFilter.started = true; postFilter.onstart();
  assert.equal(postFilter.disconnected, true);
  assert.equal(h.before.size + h.headers.size + h.redirects.size + h.errors.size, 0);
  assert.equal(h.timers.size, 0);

  const timed = await harness();
  const timeout = timed.capture({tabId: 7, url: PDF_URL});
  beginHtmlHandoff(timed);
  const get = stream(timed, 'r1', [new TextEncoder().encode('ordinary html')], {stop: false});
  beginPost(timed, 'post-1');
  const post = timed.filters.get('post-1')[0];
  const timer = [...timed.timers.values()][0];
  assert.equal(timer.delay, 60000);
  timer.fn();
  await assert.rejects(timeout, /pdf_timeout/);
  assert.equal(get.disconnected, true);
  post.started = true; post.onstart();
  assert.equal(post.disconnected, true);
  assert.equal(timed.timers.size, 0);
});

test('pre-aborted signals and navigation failures reject without leaks', async () => {
  const h = await harness();
  const controller = new AbortController(); controller.abort();
  await assert.rejects(h.capture({tabId: 7, url: PDF_URL, signal: controller.signal}), /aborted/);
  assert.equal(h.headers.size, 0);

  const failing = await harness({updateFails: true});
  await assert.rejects(failing.capture({tabId: 7, url: PDF_URL}), /pdf_navigation/);
  assert.equal(failing.headers.size + failing.redirects.size + failing.errors.size, 0);
});
