'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const {captureWithSession, withOwnedBackgroundPage, validateEndpoint, retryAfterSeconds} = require('../scripts/dev/quality/browser_response_capture.cjs');
const URL = 'https://kad.arbitr.ru/Kad/PdfDocument/11111111-2222-3333-4444-555555555555/12345678-1234-1234-1234-123456789012/act.pdf';
const ENDPOINT = 'ws://127.0.0.1:1234/devtools/browser/synthetic';
const PDF = Buffer.from('%PDF-1.7\nsynthetic fixture\n%%EOF\n');

class Session extends EventEmitter {
  constructor(status = 200, retryAfter) { super(); this.status = status; this.retryAfter = retryAfter; this.commands = []; }
  async send(method, params) {
    this.commands.push({method, params});
    if (method === 'Page.navigate') queueMicrotask(() => {
      this.emit('Network.requestWillBeSent', {type: 'Document', frameId: 'main', requestId: 'n1', request: {url: URL, method: 'GET'}});
      this.emit('Network.responseReceived', {type: 'Document', frameId: 'main', requestId: 'n1', response: {url: URL, status: this.status, mimeType: 'application/pdf', headers: {'Retry-After': this.retryAfter}}});
      this.emit('Fetch.requestPaused', {requestId: 'f1', networkId: 'n1', resourceType: 'Document', responseStatusCode: this.status, request: {url: URL}});
      this.emit('Network.loadingFinished', {requestId: 'n1', encodedDataLength: PDF.length});
    });
    if (method === 'Fetch.getResponseBody') return {body: PDF.toString('base64'), base64Encoded: true};
    if (method === 'Network.getResponseBody') throw Error('must use original Fetch stream');
    return {};
  }
}

test('maintained observer returns original bytes and continues only interception id', async () => {
  const session = new Session(), result = await captureWithSession(session, URL);
  assert.deepEqual(result.bytes, PDF); assert.equal(result.finalURL, URL);
  assert.equal(JSON.stringify(session.commands.find(c => c.method === 'Fetch.continueResponse').params), '{"requestId":"f1"}');
  assert.equal(session.commands.find(c => c.method === 'Network.enable').params.maxPostDataSize, 0);
  assert.equal(session.eventNames().length, 0);
  assert.ok(!session.commands.some(c => /Cookie|fulfill|Runtime|PostData/.test(c.method)));
});

test('403 and 429 preserve source restrictions', async () => {
  for (const [status, reason] of [[403, 'challenge'], [429, 'rate-limited']]) {
    const session = new Session(status);
    await assert.rejects(captureWithSession(session, URL), new RegExp(reason));
    assert.equal(session.eventNames().length, 0);
    assert.ok(!session.commands.some(c => c.method === 'Fetch.getResponseBody'));
  }
});

test('only explicit loopback browser WebSocket endpoint is accepted', () => {
  assert.equal(validateEndpoint(ENDPOINT), ENDPOINT);
  for (const value of ['http://127.0.0.1:1234', ENDPOINT.replace('127.0.0.1', 'remote.test'), ENDPOINT.replace('/browser/', '/page/'), ENDPOINT + '?secret=x']) assert.throws(() => validateEndpoint(value), /invalid_endpoint/);
});

test('rate limit retains only sanitized numeric server delay', async () => {
  const session = new Session(429, '3600');
  await assert.rejects(captureWithSession(session, URL), error => {
    assert.equal(error.message, 'rate-limited'); assert.equal(error.cooldownSeconds, 3600); return true;
  });
  assert.equal(session.eventNames().length, 0);
  assert.equal(retryAfterSeconds('Tue, 01 Jan 2030 01:00:00 GMT', Date.parse('Tue, 01 Jan 2030 00:00:00 GMT')), 3600);
  assert.equal(retryAfterSeconds('1'), 60);
  assert.equal(retryAfterSeconds('not-a-delay'), 60);
});

function ownedHarness(t, {prior, priorURL = URL, currentURL = URL, currentID = 'ours', closeSuccess = true, hasKad = true} = {}) {
  const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'sro-response-test-')));
  const journalPath = path.join(directory, 'target.json');
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  if (prior) fs.writeFileSync(journalPath, JSON.stringify(prior), {mode: 0o600});
  const calls = [], session = {async send() { return {targetInfo: {targetId: 'ours'}}; }, async detach() {}};
  const root = {async send(method, params) {
    calls.push({method, params});
    if (method === 'Target.getTargetInfo') return {targetInfo: params.targetId === 'prior'
      ? {targetId: 'prior', url: priorURL} : {targetId: currentID, url: currentURL}};
    if (method === 'Target.createTarget') {
      assert.equal(JSON.parse(fs.readFileSync(journalPath)).target_id, null);
      return {targetId: 'ours'};
    }
    if (method === 'Target.closeTarget') return {success: closeSuccess};
    return {};
  }, async detach() {}};
  const context = {pages() { return [{url() { return hasKad ? 'https://kad.arbitr.ru/' : 'https://unrelated.example/'; }}]; }, async waitForEvent() { return {}; }, async newCDPSession() { return session; }};
  const chromium = {async connectOverCDP() { return {contexts() { return [context]; }, async newBrowserCDPSession() { return root; }, async close() { throw Error('must never close whole browser'); }}; }};
  return {chromium, journalPath, calls};
}

test('journal exists before creation and clears only after confirmed own close', async t => {
  const h = ownedHarness(t, {currentURL: 'https://kad.arbitr.ru/Search'});
  const result = await withOwnedBackgroundPage(h.chromium, ENDPOINT, async ({recordExpectedURL}) => {
    assert.equal(JSON.parse(fs.readFileSync(h.journalPath)).target_id, 'ours');
    recordExpectedURL('https://kad.arbitr.ru/Search'); return 5;
  }, {journalPath: h.journalPath, expectedURL: URL});
  assert.equal(result, 5); assert.equal(fs.existsSync(h.journalPath), false);
  assert.deepEqual(h.calls[0], {method: 'Target.createTarget', params: {url: 'about:blank', background: true}});
});

test('recovery closes only proven prior owned target at expected URL', async t => {
  const h = ownedHarness(t, {prior: {endpoint: ENDPOINT, target_id: 'prior', expected_url: URL}});
  await withOwnedBackgroundPage(h.chromium, ENDPOINT, async () => {}, {journalPath: h.journalPath, expectedURL: URL});
  assert.deepEqual(h.calls.filter(c => c.method === 'Target.closeTarget').map(c => c.params.targetId), ['prior', 'ours']);
});

test('changed prior target URL is preserved; no new source target', async t => {
  const h = ownedHarness(t, {prior: {endpoint: ENDPOINT, target_id: 'prior', expected_url: URL}, priorURL: 'https://unrelated.example/'});
  await assert.rejects(withOwnedBackgroundPage(h.chromium, ENDPOINT, async () => {}, {journalPath: h.journalPath, expectedURL: URL}), /browser_target_unresolved/);
  assert.equal(h.calls.some(c => ['Target.closeTarget', 'Target.createTarget'].includes(c.method)), false);
  assert.equal(fs.existsSync(h.journalPath), true);
});

test('unconfirmed own close retains journal and rejects success', async t => {
  const h = ownedHarness(t, {closeSuccess: false});
  await assert.rejects(withOwnedBackgroundPage(h.chromium, ENDPOINT, async () => {}, {journalPath: h.journalPath, expectedURL: URL}), /browser_target_unresolved/);
  assert.equal(JSON.parse(fs.readFileSync(h.journalPath)).target_id, 'ours');
});

test('wrong existing context is rejected before creating target', async t => {
  const h = ownedHarness(t, {hasKad: false});
  await assert.rejects(withOwnedBackgroundPage(h.chromium, ENDPOINT, async () => {}, {journalPath: h.journalPath, expectedURL: URL}), /browser_context_unverified/);
  assert.equal(h.calls.length, 0);
});

test('current target changed by owner is preserved with unresolved journal', async t => {
  const h = ownedHarness(t, {currentURL: 'https://owner.example/edited'});
  await assert.rejects(withOwnedBackgroundPage(h.chromium, ENDPOINT, async () => {}, {journalPath: h.journalPath, expectedURL: URL}), /browser_target_unresolved/);
  assert.equal(h.calls.some(c => c.method === 'Target.closeTarget'), false);
  assert.equal(JSON.parse(fs.readFileSync(h.journalPath)).target_id, 'ours');
});

test('current target id mismatch prevents any close', async t => {
  const h = ownedHarness(t, {currentID: 'owner-target'});
  await assert.rejects(withOwnedBackgroundPage(h.chromium, ENDPOINT, async () => {}, {journalPath: h.journalPath, expectedURL: URL}), /browser_target_unresolved/);
  assert.equal(h.calls.some(c => c.method === 'Target.closeTarget'), false);
  assert.equal(fs.existsSync(h.journalPath), true);
});
