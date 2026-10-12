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

function parserDocument({forms = [], parserError = false, interactive = false, blocked = false} = {}) {
  const makeElement = (tag, attributes = {}, parentElement = null, visibility = {}) => {
    const element = {
      localName: tag, tagName: tag.toUpperCase(), parentElement,
      style: {display: visibility.display || '', visibility: visibility.visibility || ''},
      hasAttribute(name) { return name === 'hidden' ? !!visibility.hidden : Object.hasOwn(attributes, name); },
      getAttribute(name) {
        if (name === 'value') throw new Error('form values must not be read');
        return attributes[name] ?? null;
      },
      contains(other) { for (let current = other; current; current = current.parentElement) if (current === element) return true; return false; },
    };
    return element;
  };
  const formNodes = forms.map(spec => {
    const form = makeElement('form', {method: spec.method, action: spec.action, target: spec.target});
    const inputs = (spec.inputs || []).map(input => {
      const parent = input.ancestorHidden ? makeElement('div', {}, form, {hidden: input.ancestorHidden === 'attribute', display: input.ancestorHidden === 'display' ? 'none' : '', visibility: input.ancestorHidden === 'visibility' ? 'hidden' : ''}) : form;
      return makeElement('input', {type: input.type, name: input.name}, parent, {hidden: input.hidden, display: input.display || '', visibility: input.visibility || ''});
    });
    const buttons = (spec.buttons || []).map(button => makeElement('button', {type: button.type}, form,
      {hidden: button.hidden, display: button.display || '', visibility: button.visibility || ''}));
    form.querySelectorAll = selector => selector === 'input' ? inputs : [];
    return {form, inputs, controls: [...inputs, ...buttons]};
  });
  const outsideControls = interactive ? [makeElement('button', {type: 'button'})] : [];
  return {
    querySelector(selector) {
      if (selector === 'parsererror' && parserError) return {};
      return blocked && selector !== 'parsererror' ? {} : null;
    },
    querySelectorAll(selector) {
      if (selector === 'form') return formNodes.map(item => item.form);
      if (selector === 'input,button,select,textarea,[contenteditable]') return [...formNodes.flatMap(item => item.controls), ...outsideControls];
      return [];
    },
  };
}

async function harness({available = true, updateFails = false, document = parserDocument(), parserAvailable = true} = {}) {
  const before = event();
  const headers = event();
  const redirects = event();
  const errors = event();
  const filters = new Map();
  const updates = [];
  const timers = new Map();
  const diagnostics = [];
  let nextTimer = 1;
  const webRequest = {
    onBeforeRequest: before,
    onHeadersReceived: headers,
    onBeforeRedirect: redirects,
    onErrorOccurred: errors,
    ...(available ? {filterResponseData(id) {
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
    console: {info(...args) { diagnostics.push(args); }},
    setTimeout(fn, delay) { const id = nextTimer++; timers.set(id, {fn, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  if (parserAvailable) sandbox.DOMParser = class { parseFromString() { return document; } };
  sandbox.browser = api;
  const context = vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL('native-pdf.js', ROOT), 'utf8'), context, {filename: 'native-pdf.js'});
  return {capture: context.SroKadNativePdf.capture, before, headers, redirects, errors, filters, updates, timers, diagnostics};
}

function details(overrides = {}) {
  return {tabId: 7, type: 'main_frame', frameId: 0, method: 'GET', requestId: 'r1', url: PDF_URL,
    statusCode: 200, responseHeaders: [{name: 'Content-Type', value: 'application/pdf'}], ...overrides};
}

function stream(h, requestId = 'r1', parts = [bytes]) {
  const list = h.filters.get(requestId);
  const filter = list?.[list.length - 1];
  assert.ok(filter);
  filter.started = true;
  filter.onstart();
  for (const part of parts) filter.ondata({data: part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength)});
  filter.onstop();
  return filter;
}

function begin(h, overrides = {}) { h.before.fire(details(overrides)); }

test('captures exact PDF bytes while forwarding each original chunk unchanged', async () => {
  const h = await harness();
  const resultPromise = h.capture({tabId: 7, url: PDF_URL});
  assert.equal(JSON.stringify(h.updates), JSON.stringify([{id: 7, url: PDF_URL}]));
  assert.equal(h.headers.size, 1);
  assert.equal(JSON.stringify(h.headers.lastOptions[0]), JSON.stringify({urls: ['https://kad.arbitr.ru/*'], types: ['main_frame'], tabId: 7}));
  assert.equal(JSON.stringify(h.headers.lastOptions[1]), JSON.stringify(['blocking', 'responseHeaders']));
  assert.equal(JSON.stringify(h.before.lastOptions[0]), JSON.stringify(h.headers.lastOptions[0]));
  assert.equal(JSON.stringify(h.before.lastOptions[1]), JSON.stringify(['blocking']));
  begin(h);
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
  begin(h);
  // The 302 response is ignored, then the same request ID begins a new response.
  h.headers.fire(details({statusCode: 302}));
  const redirectResponse = h.filters.get('r1')[0];
  redirectResponse.started = true;
  redirectResponse.onstart();
  redirectResponse.ondata({data: bytes.buffer.slice(0)});
  redirectResponse.onstop();
  assert.equal(redirectResponse.disconnected, true);
  h.redirects.fire(details({statusCode: 302, redirectUrl: PDF_REDIRECT}));
  begin(h, {url: PDF_REDIRECT});
  h.headers.fire(details({url: PDF_REDIRECT, statusCode: 200}));
  stream(h);
  assert.deepEqual([...await resultPromise], [...bytes]);

  for (const redirectUrl of [OTHER_HOST, PDF_REDIRECT.replace('?isAddStamp=True', '?other=true'),
    PDF_REDIRECT.replace('/Document/Pdf/', '/other/')]) {
    const bad = await harness();
    const pending = bad.capture({tabId: 7, url: PDF_URL});
    begin(bad);
    bad.headers.fire(details({statusCode: 302}));
    const redirectFilter = bad.filters.get('r1')[0];
    redirectFilter.started = true;
    redirectFilter.onstart();
    bad.redirects.fire(details({statusCode: 302, redirectUrl}));
    await assert.rejects(pending, /pdf_redirect/);
    assert.equal(redirectFilter.disconnected, true);
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
  h.before.fire(details({tabId: 8}));
  h.before.fire(details({frameId: 1}));
  h.before.fire(details({type: 'xmlhttprequest'}));
  h.before.fire(details({url: OTHER_HOST}));
  begin(h);
  h.headers.fire(details({tabId: 8}));
  h.headers.fire(details({frameId: 1}));
  h.headers.fire(details({type: 'xmlhttprequest'}));
  h.headers.fire(details({url: OTHER_HOST}));
  assert.equal(h.filters.size, 1);
  h.headers.fire(details());
  h.errors.fire(details({requestId: 'other'}));
  assert.equal(h.filters.size, 1);
  stream(h);
  assert.deepEqual([...await pending], [...bytes]);
});

test('stops explicitly on HTML, authentication challenge, rate limit and other HTTP failures', async () => {
  for (const [overrides, reason] of [
    [{statusCode: 401}, 'challenge'], [{statusCode: 403}, 'challenge'], [{statusCode: 429}, 'rate-limited'],
    [{statusCode: 500}, 'pdf_http'],
  ]) {
    const h = await harness();
    const pending = h.capture({tabId: 7, url: PDF_URL});
    begin(h);
    h.headers.fire(details(overrides));
    await assert.rejects(pending, new RegExp(reason));
    assert.equal(h.filters.size, 1);
    assert.equal(h.headers.size, 0);
  }
});

test('rejects oversized data and invalid PDF magic, disconnecting the filter', async () => {
  const large = await harness();
  const tooBig = large.capture({tabId: 7, url: PDF_URL});
  begin(large);
  large.headers.fire(details());
  const oversized = new Uint8Array(25 * 1024 * 1024 + 1);
  const filter = large.filters.get('r1')[0];
  filter.started = true;
  filter.onstart();
  filter.ondata({data: oversized.buffer});
  await assert.rejects(tooBig, /pdf_too_large/);
  assert.equal(filter.disconnected, true);

  const invalid = await harness();
  const notPdf = invalid.capture({tabId: 7, url: PDF_URL});
  begin(invalid);
  invalid.headers.fire(details());
  const badFilter = stream(invalid, 'r1', [new TextEncoder().encode('<html>')]);
  await assert.rejects(notPdf, /not_pdf/);
  assert.equal(badFilter.disconnected, true);
});

test('rejects duplicate request IDs and network errors for the owned request', async () => {
  const duplicate = await harness();
  const pendingDuplicate = duplicate.capture({tabId: 7, url: PDF_URL});
  begin(duplicate);
  duplicate.headers.fire(details());
  begin(duplicate, {requestId: 'r2'});
  const duplicateFilter = duplicate.filters.get('r1')[0];
  duplicateFilter.started = true;
  duplicateFilter.onstart();
  duplicate.headers.fire(details({requestId: 'r2'}));
  await assert.rejects(pendingDuplicate, /pdf_duplicate_request/);
  assert.equal(duplicateFilter.disconnected, true);

  const network = await harness();
  const pendingNetwork = network.capture({tabId: 7, url: PDF_URL});
  begin(network);
  network.errors.fire(details());
  await assert.rejects(pendingNetwork, /pdf_network/);
  const beforeHeaders = await harness();
  const pendingBeforeHeaders = beforeHeaders.capture({tabId: 7, url: PDF_URL});
  begin(beforeHeaders);
  beforeHeaders.errors.fire(details());
  await assert.rejects(pendingBeforeHeaders, /pdf_network/);
});

test('cancels and times out with listeners, timer and captured chunks released', async () => {
  const cancelled = await harness();
  const controller = new AbortController();
  const pendingCancel = cancelled.capture({tabId: 7, url: PDF_URL, signal: controller.signal});
  begin(cancelled);
  cancelled.headers.fire(details());
  const cancelFilter = cancelled.filters.get('r1')[0];
  cancelFilter.started = true;
  cancelFilter.onstart();
  cancelFilter.ondata({data: bytes.buffer.slice(0, 2)});
  controller.abort();
  await assert.rejects(pendingCancel, /aborted/);
  assert.equal(cancelFilter.disconnected, true);
  assert.equal(cancelled.headers.size + cancelled.redirects.size + cancelled.errors.size, 0);
  assert.equal(cancelled.timers.size, 0);

  const beforeStart = await harness();
  const pendingBeforeStart = beforeStart.capture({tabId: 7, url: PDF_URL});
  begin(beforeStart);
  beforeStart.headers.fire(details());
  const earlyFilter = beforeStart.filters.get('r1')[0];
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

function hiddenForm({method = 'post', action = PDF_REDIRECT, inputs = [
  {type: 'hidden', name: 'token'}, {type: 'hidden', name: 'hash'},
], interactive = false} = {}) {
  return parserDocument({forms: [{method, action, inputs, buttons: interactive ? [{type: 'submit'}] : []}]});
}

test('passes the bounded final GET form through, then captures exactly one browser POST with a new request ID', async () => {
  const h = await harness({document: hiddenForm()});
  const pending = h.capture({tabId: 7, url: PDF_URL});
  beginFinalResponse(h);
  const html = new TextEncoder().encode('<html>synthetic handoff body</html>');
  h.headers.fire(details({url: PDF_REDIRECT, responseHeaders: [{name: 'Content-Type', value: 'text/html; charset=utf-8'}]}));
  const getFilter = stream(h, 'r1', [html.subarray(0, 9), html.subarray(9)]);
  assert.deepEqual(getFilter.writes.flatMap(chunk => [...chunk]), [...html]);
  assert.equal(h.headers.size, 1, 'capture remains active while awaiting natural browser POST');
  h.before.fire(details({url: PDF_REDIRECT, requestId: 'browser-post', method: 'POST'}));
  h.headers.fire(details({url: PDF_REDIRECT, requestId: 'browser-post', method: 'POST',
    responseHeaders: [{name: 'Content-Type', value: 'application/pdf'}]}));
  const postFilter = stream(h, 'browser-post');
  assert.deepEqual([...await pending], [...bytes]);
  assert.deepEqual(postFilter.writes.flatMap(chunk => [...chunk]), [...bytes]);
  assert.equal(postFilter.closed, true);
  assert.equal(h.headers.size + h.before.size + h.redirects.size + h.errors.size, 0);
  assert.equal(JSON.stringify(h.diagnostics).includes('token'), false);
});

test('accepts token text fields only when explicit hidden attributes or inline hidden styles prove invisibility', async () => {
  for (const token of [
    {type: 'text', name: 'token', hidden: true},
    {type: 'text', name: 'token', display: 'none'},
    {type: 'text', name: 'token', visibility: 'hidden'},
    {type: 'text', name: 'token', ancestorHidden: 'attribute'},
    {type: 'text', name: 'token', ancestorHidden: 'display'},
  ]) {
    const h = await harness({document: hiddenForm({inputs: [token, {type: 'hidden', name: 'hash'}]})});
    const pending = h.capture({tabId: 7, url: PDF_URL});
    beginFinalResponse(h);
    const html = new TextEncoder().encode('<html>synthetic handoff</html>');
    h.headers.fire(details({url: PDF_REDIRECT, responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}));
    stream(h, 'r1', [html]);
    h.before.fire(details({url: PDF_REDIRECT, requestId: 'post-hidden', method: 'POST'}));
    h.headers.fire(details({url: PDF_REDIRECT, requestId: 'post-hidden', method: 'POST',
      responseHeaders: [{name: 'Content-Type', value: 'application/pdf'}]}));
    stream(h, 'post-hidden');
    assert.deepEqual([...await pending], [...bytes]);
  }
});

test('fails closed for missing/multiple forms, wrong method/action, controls, unsupported fields and parser errors', async () => {
  const cases = [
    [parserDocument(), 'pdf_handoff_missing_form', 'missing_form'],
    [parserDocument({forms: [{method: 'post', action: PDF_REDIRECT, inputs: []}, {method: 'post', action: PDF_REDIRECT, inputs: []}]}), 'pdf_handoff_multiple_forms', 'multiple_forms'],
    [hiddenForm({method: 'get'}), 'pdf_handoff_method_mismatch', 'method_mismatch'],
    [hiddenForm({action: `${PDF_REDIRECT}&other=1`}), 'pdf_handoff_action_mismatch', 'action_mismatch'],
    [hiddenForm({interactive: true}), 'pdf_handoff_interactive_control', 'interactive_control'],
    [parserDocument({forms: [{method: 'post', action: PDF_REDIRECT, inputs: [
      {type: 'hidden', name: 'token'}, {type: 'hidden', name: 'hash'},
    ]}], interactive: true}), 'pdf_handoff_interactive_control', 'interactive_control'],
    [parserDocument({forms: [{method: 'post', action: PDF_REDIRECT, target: '_blank', inputs: [
      {type: 'hidden', name: 'token'}, {type: 'hidden', name: 'hash'},
    ]}]}), 'pdf_handoff_action_mismatch', 'action_mismatch'],
    [hiddenForm({inputs: [{type: 'text', name: 'token'}]}), 'pdf_handoff_interactive_control', 'interactive_control'],
    [hiddenForm({inputs: [{type: 'text', name: 'token', ancestorHidden: 'visibility'}, {type: 'hidden', name: 'hash'}]}), 'pdf_handoff_interactive_control', 'interactive_control'],
    [hiddenForm({inputs: [{type: 'hidden', name: 'secret'}]}), 'pdf_handoff_unsupported_fields', 'unsupported_fields'],
    [parserDocument({parserError: true}), 'pdf_handoff_malformed_document', 'malformed_document'],
  ];
  for (const [document, code, reason] of cases) {
    const h = await harness({document});
    const pending = h.capture({tabId: 7, url: PDF_URL});
    beginFinalResponse(h);
    h.headers.fire(details({url: PDF_REDIRECT, responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}));
    const filter = stream(h, 'r1', [new TextEncoder().encode('SYNTHETIC HTML SECRET')]);
    await assert.rejects(pending, new RegExp(code));
    assert.equal(filter.disconnected, true);
    assert.equal(h.before.size + h.headers.size + h.redirects.size + h.errors.size, 0);
    assert.equal(h.diagnostics.length, 1);
    const [eventName, encoded] = h.diagnostics[0];
    assert.equal(eventName, 'kad-pdf-handoff');
    const record = JSON.parse(encoded);
    assert.deepEqual(Object.keys(record).sort(), ['ancestorHidden', 'controlCount', 'formCount', 'inputCount', 'reason']);
    assert.equal(typeof record.ancestorHidden, 'boolean');
    assert.equal(Number.isInteger(record.formCount), true);
    assert.equal(Number.isInteger(record.controlCount), true);
    assert.equal(Number.isInteger(record.inputCount), true);
    assert.equal(record.reason, reason);
    assert.equal(JSON.stringify(record).includes('SECRET'), false);
  }
});

test('stops explicitly when a structural HTML parser is unavailable', async () => {
  const h = await harness({parserAvailable: false});
  const pending = h.capture({tabId: 7, url: PDF_URL});
  beginFinalResponse(h);
  h.headers.fire(details({url: PDF_REDIRECT, responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}));
  stream(h, 'r1', [new TextEncoder().encode('handoff')]);
  await assert.rejects(pending, /pdf_handoff_unsupported_parser/);
  assert.equal(JSON.parse(h.diagnostics[0][1]).reason, 'unsupported_parser');
});

test('challenge HTML is rejected before parser handoff and oversized or malformed HTML stops', async () => {
  const challenge = await harness({document: hiddenForm()});
  const challengePending = challenge.capture({tabId: 7, url: PDF_URL});
  beginFinalResponse(challenge);
  challenge.headers.fire(details({url: PDF_REDIRECT, responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}));
  const challengeFilter = stream(challenge, 'r1', [new TextEncoder().encode('<html>CAPTCHA challenge-platform</html>')]);
  await assert.rejects(challengePending, /pdf_html_challenge/);
  assert.equal(challengeFilter.disconnected, true);
  assert.equal(challenge.diagnostics[0][0], 'kad-pdf-handoff');
  assert.equal(JSON.parse(challenge.diagnostics[0][1]).reason, 'challenge_marker');

  const tooLarge = await harness({document: hiddenForm()});
  const largePending = tooLarge.capture({tabId: 7, url: PDF_URL});
  beginFinalResponse(tooLarge);
  tooLarge.headers.fire(details({url: PDF_REDIRECT, responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}));
  const f = tooLarge.filters.get('r1')[1];
  f.started = true; f.onstart();
  f.ondata({data: new Uint8Array(128 * 1024 + 1).buffer});
  await assert.rejects(largePending, /pdf_handoff_too_large/);
  assert.equal(f.disconnected, true);

  const malformed = await harness({document: hiddenForm()});
  const malformedPending = malformed.capture({tabId: 7, url: PDF_URL});
  beginFinalResponse(malformed);
  malformed.headers.fire(details({url: PDF_REDIRECT, responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}));
  const malformedFilter = stream(malformed, 'r1', [new Uint8Array([0xff, 0xfe])]);
  await assert.rejects(malformedPending, /pdf_handoff_malformed/);
  assert.equal(malformedFilter.disconnected, true);
});

test('rejects POST without the validated handoff, duplicate POST, mismatched navigation, and non-PDF POST response', async () => {
  const early = await harness();
  const earlyPending = early.capture({tabId: 7, url: PDF_URL});
  begin(early, {method: 'POST'});
  await assert.rejects(earlyPending, /pdf_handoff_mismatch/);

  const duplicate = await harness({document: hiddenForm()});
  const duplicatePending = duplicate.capture({tabId: 7, url: PDF_URL});
  beginFinalResponse(duplicate);
  duplicate.headers.fire(details({url: PDF_REDIRECT, responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}));
  stream(duplicate, 'r1', [new TextEncoder().encode('handoff')]);
  duplicate.before.fire(details({url: PDF_REDIRECT, requestId: 'post-1', method: 'POST'}));
  duplicate.before.fire(details({url: PDF_REDIRECT, requestId: 'post-2', method: 'POST'}));
  await assert.rejects(duplicatePending, /pdf_handoff_mismatch/);

  const wrongUrl = await harness({document: hiddenForm()});
  const wrongUrlPending = wrongUrl.capture({tabId: 7, url: PDF_URL});
  beginFinalResponse(wrongUrl);
  wrongUrl.headers.fire(details({url: PDF_REDIRECT, responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}));
  stream(wrongUrl, 'r1', [new TextEncoder().encode('handoff')]);
  wrongUrl.before.fire(details({url: PDF_REDIRECT.replace('act.pdf', 'other.pdf'), requestId: 'post-1', method: 'POST'}));
  await assert.rejects(wrongUrlPending, /pdf_navigation_mismatch/);

  const wrongMime = await harness({document: hiddenForm()});
  const wrongMimePending = wrongMime.capture({tabId: 7, url: PDF_URL});
  beginFinalResponse(wrongMime);
  wrongMime.headers.fire(details({url: PDF_REDIRECT, responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}));
  stream(wrongMime, 'r1', [new TextEncoder().encode('handoff')]);
  wrongMime.before.fire(details({url: PDF_REDIRECT, requestId: 'post-1', method: 'POST'}));
  wrongMime.headers.fire(details({url: PDF_REDIRECT, requestId: 'post-1', method: 'POST',
    responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}));
  await assert.rejects(wrongMimePending, /pdf_mime/);

  for (const [postId, status, expected] of [
    ['r1', 200, 'pdf_handoff_mismatch'], ['post-1', 206, 'pdf_http'], ['post-1', 302, 'pdf_post_redirect'],
  ]) {
    const badPost = await harness({document: hiddenForm()});
    const badPending = badPost.capture({tabId: 7, url: PDF_URL});
    beginFinalResponse(badPost);
    badPost.headers.fire(details({url: PDF_REDIRECT, responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}));
    stream(badPost, 'r1', [new TextEncoder().encode('handoff')]);
    badPost.before.fire(details({url: PDF_REDIRECT, requestId: postId, method: 'POST'}));
    badPost.headers.fire(details({url: PDF_REDIRECT, requestId: postId, method: 'POST', statusCode: status,
      responseHeaders: [{name: 'Content-Type', value: status === 302 ? 'text/html' : 'application/pdf'}]}));
    await assert.rejects(badPending, new RegExp(expected));
  }
});

test('stop and the shared 60 second deadline cover both handoff phases', async () => {
  const h = await harness({document: hiddenForm()});
  const controller = new AbortController();
  const pending = h.capture({tabId: 7, url: PDF_URL, signal: controller.signal});
  beginFinalResponse(h);
  h.headers.fire(details({url: PDF_REDIRECT, responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}));
  stream(h, 'r1', [new TextEncoder().encode('handoff')]);
  controller.abort();
  await assert.rejects(pending, /aborted/);
  assert.equal(h.headers.size + h.before.size + h.redirects.size + h.errors.size, 0);
  assert.equal(h.timers.size, 0);

  const timed = await harness({document: hiddenForm()});
  const timeoutPending = timed.capture({tabId: 7, url: PDF_URL});
  beginFinalResponse(timed);
  timed.headers.fire(details({url: PDF_REDIRECT, responseHeaders: [{name: 'Content-Type', value: 'text/html'}]}));
  stream(timed, 'r1', [new TextEncoder().encode('handoff')]);
  const timer = [...timed.timers.values()][0];
  assert.equal(timer.delay, 60000);
  timer.fn();
  await assert.rejects(timeoutPending, /pdf_timeout/);
  assert.equal(timed.before.size + timed.headers.size + timed.redirects.size + timed.errors.size, 0);
});
