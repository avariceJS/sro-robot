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

class SyntheticElement {
  constructor(tagName = 'div') { this.tagName = tagName; this.children = []; this.listeners = {}; this.disabled = false; this.textContent = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute() {}
  addEventListener(type, listener) { this.listeners[type] = listener; }
  querySelectorAll(selector) { return selector === 'button' ? this.children.flatMap(node => node.tagName === 'button' ? [node] : node.children.filter(child => child.tagName === 'button')) : []; }
}

async function harness({initialPending = null, resumed = null, receiptQueue = [], importQueue = [], bridgeStatus = null} = {}) {
  const messages = [];
  const requests = [];
  const session = new Map();
  if (initialPending) session.set(STORAGE_KEY, JSON.stringify(initialPending));
  const listeners = new Map();
  const status = {textContent: ''};
  const choices = new SyntheticElement();
  const button = {disabled: false};
  const form = {
    dataset: {},
    elements: {case_number: {value: CASE}, act_date: {value: ''}},
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
      case 'bridge-select': return {phase: 'acquiring'};
      case 'bridge-status':
        if (Array.isArray(bridgeStatus)) return bridgeStatus.shift();
        return bridgeStatus || {phase: 'ready', body: BODY, source: SOURCE};
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
    document: {createElement(tag) { return new SyntheticElement(tag); }, querySelector(selector) {
      return selector === '#kad-browser-search' ? form : selector === '#kad-browser-status' ? status : selector === '#kad-candidate-choices' ? choices : null;
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
  return {messages, requests, session, status, choices, button, form, location, submit, context};
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

test('a gateway reload notice replaces the generic searching status until the next poll', async () => {
  const h = await harness({
    bridgeStatus: [
      {phase: 'searching', notice: 'КАД временно недоступен. Обновляю страницу…'},
      {phase: 'ready', body: BODY, source: SOURCE},
    ],
    importQueue: [{ok: true, status: 201, async json() { return {task_url: '/tasks/7'}; }}],
  });
  const pending = h.submit();
  await new Promise(resolve => setTimeout(resolve, 100));
  assert.equal(h.status.textContent, 'КАД временно недоступен. Обновляю страницу…');
  await pending;
  assert.deepEqual(h.location.assigned, ['/tasks/7']);
});

test('ambiguous KAD inventory is shown in the local service and explicit selection continues import', async () => {
  const candidates = [
    {url: `${SOURCE.cardUrl.replace('/Card/', '/Kad/PdfDocument/')}/11111111-2222-3333-4444-555555555555/decision.pdf`, label: 'Synthetic decision', actDate: '2024-02-29'},
    {url: `${SOURCE.cardUrl.replace('/Card/', '/Kad/PdfDocument/')}/22222222-2222-3333-4444-555555555555/definition.pdf`, label: 'Synthetic definition', actDate: '2024-03-01'},
  ];
  const h = await harness({
    bridgeStatus: [
      {phase: 'documents', caseNumber: CASE, requestedDate: null, candidates},
      {phase: 'ready', body: BODY, source: {...SOURCE, candidates, selectedUrl: candidates[1].url, selectionReason: 'employee_selected_visible_document'}},
    ],
    importQueue: [{ok: true, status: 201, async json() { return {task_url: '/tasks/81'}; }}],
  });
  const submitted = h.submit();
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.match(h.status.textContent, /видимые PDF/);
  const buttons = h.choices.querySelectorAll('button');
  assert.equal(buttons.length, 2);
  assert.match(buttons[0].textContent, /Synthetic decision/);
  assert.match(buttons[1].textContent, /01\.03\.2024/);
  await buttons[1].listeners.click();
  await submitted;
  const choiceMessage = h.messages.find(message => message.type === 'bridge-select');
  assert.equal(choiceMessage.url, candidates[1].url);
  assert.deepEqual(h.location.assigned, ['/tasks/81']);
});

test('the service requires explicit confirmation even when only one visible PDF is returned', async () => {
  const candidate = {url: 'https://kad.arbitr.ru/Kad/PdfDocument/11111111-2222-3333-4444-555555555555/11111111-2222-3333-4444-555555555555/decision.pdf', label: 'Synthetic sole visible act', actDate: '2024-02-29'};
  const h = await harness({
    bridgeStatus: [
      {phase: 'documents', caseNumber: CASE, requestedDate: null, candidateInventoryComplete: false, candidates: [candidate]},
      {phase: 'ready', body: BODY, source: {...SOURCE, candidates: [candidate], selectedUrl: candidate.url, selectionReason: 'employee_selected_visible_document', candidateInventoryComplete: false}},
    ],
    importQueue: [{ok: true, status: 201, async json() { return {task_url: '/tasks/82'}; }}],
  });
  const submitted = h.submit();
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal(h.choices.querySelectorAll('button').length, 1);
  assert.match(h.choices.querySelectorAll('button')[0].textContent, /Synthetic sole visible act/);
  assert.equal(h.messages.some(message => message.type === 'bridge-select'), false);
  await h.choices.querySelectorAll('button')[0].listeners.click();
  await submitted;
  assert.equal(h.messages.find(message => message.type === 'bridge-select').url, candidate.url);
  assert.deepEqual(h.location.assigned, ['/tasks/82']);
});

test('a requested act date with no visible match stops without importing or starting another search', async () => {
  const candidate = {url: 'https://kad.arbitr.ru/Kad/PdfDocument/11111111-2222-3333-4444-555555555555/11111111-2222-3333-4444-555555555555/decision.pdf', label: 'Synthetic act'};
  const h = await harness({bridgeStatus: {phase: 'documents', caseNumber: CASE, requestedDate: '2024-02-29', candidates: [candidate]}});
  await h.submit();
  assert.match(h.status.textContent, /Ничего не импортировано/);
  assert.equal(h.requests.filter(request => request.url === '/kad-helper/import').length, 0);
  assert.equal(h.messages.filter(message => message.type === 'bridge-start').length, 1);
  assert.equal(h.messages.filter(message => message.type === 'bridge-select').length, 0);
  assert.equal(h.messages.find(message => message.type === 'bridge-finish').keepTab, true);
  assert.equal(h.session.has(STORAGE_KEY), false);
});

test('an empty visible PDF inventory stops promptly without claiming the case has no documents', async () => {
  const h = await harness({bridgeStatus: {phase: 'documents', caseNumber: CASE, requestedDate: null, candidates: []}});

  await h.submit();

  assert.match(h.status.textContent, /не видно PDF/);
  assert.match(h.status.textContent, /Полнота списка не подтверждена/);
  assert.equal(h.requests.filter(request => request.url === '/kad-helper/import').length, 0);
  assert.equal(h.messages.filter(message => message.type === 'bridge-start').length, 1);
  assert.equal(h.messages.filter(message => message.type === 'bridge-select').length, 0);
  assert.equal(h.messages.find(message => message.type === 'bridge-finish').keepTab, true);
  assert.equal(h.session.has(STORAGE_KEY), false);
});


test('a closed KAD tab reports a terminal failure without posting a local import', async () => {
  const reason = 'Вкладка КАД закрыта. Передача документа не выполнена; запустите новый поиск.';
  const h = await harness({bridgeStatus: {phase: 'stopped', reason}});

  await h.submit();

  assert.equal(h.status.textContent, reason);
  assert.equal(h.requests.filter(request => request.url === '/kad-helper/import').length, 0);
  assert.equal(h.messages.filter(message => message.type === 'bridge-importing').length, 0);
  assert.equal(h.messages.filter(message => message.type === 'bridge-finish').length, 1);
  assert.equal(h.messages.find(message => message.type === 'bridge-finish').keepTab, true);
  assert.equal(h.session.has(STORAGE_KEY), false);
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
