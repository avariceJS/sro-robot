import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const CASE = 'А99-12345/2099';
const RUN = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
const IDEMPOTENCY_KEY = 'aaaaaaaabbbbccccddddeeeeeeeeeeee0000000000000000';
const STORAGE_KEY = 'sro-kad-pending-v1';
const ROOT = new URL('../browser-extension/kad-helper/', import.meta.url);
const SOURCE = {requestId: RUN, caseNumber: CASE, cardUrl: `https://kad.arbitr.ru/Card/11111111-2222-3333-4444-555555555555`};
const BODY = Buffer.from('%PDF-1.7 synthetic fixture').toString('base64');

class SyntheticFormData {
  constructor() { this.entries = []; }
  append(...entry) { this.entries.push(entry); }
  get(name) { return this.entries.find(([key]) => key === name)?.[1]; }
}

class SyntheticBlob {
  constructor(parts, options = {}) { this.parts = parts; this.type = options.type || ''; }
}

async function harness({initialPending = null, resumed = null, receiptQueue = [], importQueue = [], bridgeStatus = null} = {}) {
  const messages = [];
  const requests = [];
  const session = new Map();
  if (initialPending) session.set(STORAGE_KEY, JSON.stringify(initialPending));
  const listeners = new Map();
  const status = {textContent: ''};
  const button = {disabled: false};
  const form = {
    dataset: {},
    elements: {case_number: {value: CASE}},
    querySelector(selector) { return selector === 'button[type=submit]' ? button : null; },
    addEventListener(type, fn) { listeners.set(type, fn); },
  };
  const location = {
    origin: 'http://localhost:8000',
    pathname: '/',
    assigned: [],
    assign(url) { this.assigned.push(url); },
  };
  const extensionApi = {runtime: {async sendMessage(message) {
    messages.push({...message});
    switch (message.type) {
      case 'bridge-resume': return resumed || {requestId: null};
      case 'bridge-start': return {phase: 'searching'};
      case 'bridge-status': return bridgeStatus || {phase: 'ready', body: BODY, source: SOURCE};
      case 'bridge-importing': return {ok: true};
      case 'bridge-finish': return {ok: true};
      default: return {error: 'unknown synthetic message'};
    }
  }}};
  const fetchImpl = async (url, options) => {
    requests.push({url, options});
    if (url.startsWith('/kad-helper/receipt/')) {
      const response = receiptQueue.length ? receiptQueue.shift() : {status: 404, ok: false};
      if (response instanceof Error) throw response;
      return response;
    }
    if (url === '/kad-helper/import') {
      const response = importQueue.length ? importQueue.shift() : new Error('synthetic unknown POST result');
      if (response instanceof Error) throw response;
      return response;
    }
    throw new Error(`unexpected synthetic URL: ${url}`);
  };
  const context = vm.createContext({
    URL,
    Promise,
    Error,
    Date,
    Uint8Array,
    AbortSignal: {timeout() { return {syntheticTimeout: true}; }},
    FormData: SyntheticFormData,
    Blob: SyntheticBlob,
    atob,
    crypto: {randomUUID: () => RUN},
    browser: extensionApi,
    document: {querySelector(selector) {
      return selector === '#kad-browser-search' ? form : selector === '#kad-browser-status' ? status : null;
    }},
    sessionStorage: {
      getItem(key) { return session.get(key) ?? null; },
      setItem(key, value) { session.set(key, String(value)); },
      removeItem(key) { session.delete(key); },
    },
    location,
    fetch: fetchImpl,
    setTimeout,
    clearTimeout,
  });
  const source = await readFile(new URL('service.js', ROOT), 'utf8');
  vm.runInContext(source, context, {filename: 'service.js'});
  async function submit() {
    const listener = listeners.get('submit');
    assert.equal(typeof listener, 'function', 'service bridge installs a submit handler');
    let prevented = false;
    let stopped = false;
    await listener({
      preventDefault() { prevented = true; },
      stopImmediatePropagation() { stopped = true; },
    });
    assert.equal(prevented, true);
    assert.equal(stopped, true);
  }
  return {messages, requests, session, status, button, form, location, submit, context};
}

test('unknown POST retains the run; explicit retry checks receipt and reuses its idempotency key', async () => {
  const h = await harness({
    receiptQueue: [{status: 404, ok: false}, {status: 404, ok: false}],
    importQueue: [new Error('connection lost after POST'), {ok: true, status: 201, async json() { return {task_url: '/tasks/123'}; }}],
  });
  await h.submit();
  const pending = JSON.parse(h.session.get(STORAGE_KEY));
  assert.deepEqual(pending, {requestId: RUN, importStarted: true});
  assert.equal(h.location.assigned.length, 0, 'unknown POST outcome does not navigate or discard its run');
  assert.equal(h.messages.filter(message => message.type === 'bridge-start').length, 1);
  assert.equal(h.messages.filter(message => message.type === 'bridge-importing').length, 1);

  await h.submit();
  const starts = h.messages.filter(message => message.type === 'bridge-start');
  assert.equal(starts.length, 1, 'explicit retry resumes the existing run instead of starting another KAD search');
  const receiptRequests = h.requests.filter(request => request.url.startsWith('/kad-helper/receipt/'));
  assert.equal(receiptRequests.length, 2, 'one receipt lookup follows the unknown POST and another precedes explicit retry');
  const imports = h.requests.filter(request => request.url === '/kad-helper/import');
  assert.equal(imports.length, 2);
  assert.deepEqual(imports.map(({options}) => options.body.get('idempotency_key')), [IDEMPOTENCY_KEY, IDEMPOTENCY_KEY]);
  assert.ok(imports.every(({options}) => options.redirect === 'error' && options.credentials === 'same-origin'));
  assert.equal(h.messages.filter(message => message.type === 'bridge-importing').length, 2);
  assert.equal(h.messages.filter(message => message.type === 'bridge-finish').length, 1);
  assert.equal(h.session.has(STORAGE_KEY), false);
  assert.deepEqual(h.location.assigned, ['/tasks/123']);
});

test('a recovered pending run checks its local receipt first and finishes by navigating to that task', async () => {
  const h = await harness({
    initialPending: {requestId: RUN, importStarted: true},
    resumed: {requestId: RUN, importStarted: true},
    receiptQueue: [{status: 200, ok: true, async json() { return {task_url: '/tasks/456'}; }}],
  });
  await h.submit();
  assert.equal(h.messages.filter(message => message.type === 'bridge-resume').length, 1);
  assert.equal(h.messages.filter(message => message.type === 'bridge-start').length, 0);
  assert.equal(h.messages.filter(message => message.type === 'bridge-status').length, 0);
  assert.equal(h.messages.filter(message => message.type === 'bridge-finish').length, 1);
  assert.equal(h.requests.filter(request => request.url === '/kad-helper/import').length, 0);
  assert.equal(h.requests.filter(request => request.url === `/kad-helper/receipt/${IDEMPOTENCY_KEY}`).length, 1);
  assert.equal(h.session.has(STORAGE_KEY), false);
  assert.deepEqual(h.location.assigned, ['/tasks/456']);
});

test('bridge resume can reconstruct a pending run when page session storage is empty', async () => {
  const h = await harness({resumed: {requestId: RUN, importStarted: true}});
  await h.submit();
  assert.equal(h.messages.filter(message => message.type === 'bridge-start').length, 0);
  assert.equal(h.requests[0].url, `/kad-helper/receipt/${IDEMPOTENCY_KEY}`);
  assert.equal(JSON.parse(h.session.get(STORAGE_KEY)).requestId, RUN);
});

test('capture failure before import clears the pending run and retains the KAD card tab', async () => {
  const h = await harness({bridgeStatus: {phase: 'stopped', reason: 'Synthetic capture failure'}});

  await h.submit();

  assert.equal(h.status.textContent, 'Synthetic capture failure');
  assert.equal(h.session.has(STORAGE_KEY), false);
  assert.equal(h.requests.filter(request => request.url === '/kad-helper/import').length, 0);
  const finish = h.messages.find(message => message.type === 'bridge-finish');
  assert.ok(finish);
  assert.equal(finish.keepTab, true);
});
