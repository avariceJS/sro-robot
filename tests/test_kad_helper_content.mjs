import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const CASE = 'А99-12345/2099';
const RUN = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
const CARD_ID = '11111111-2222-3333-4444-555555555555';
const CARD = `https://kad.arbitr.ru/Card/${CARD_ID}`;
const PDF = `https://kad.arbitr.ru/Kad/PdfDocument/${CARD_ID}/12345678-1234-1234-1234-123456789012/decision.pdf`;
const CONTENT_URL = new URL('../browser-extension/kad-helper/content.js', import.meta.url);

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.listeners = {};
    this.style = {};
    this.attributes = {};
    this.textContent = '';
  }
  append(...elements) { this.children.push(...elements); }
  replaceChildren(...elements) { this.children = [...elements]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  setAttribute(name, value) { this.attributes[name] = value; }
  click() {
    let prevented = false;
    const event = { preventDefault() { prevented = true; } };
    for (const fn of this.listeners.click || []) fn(event);
    return { prevented };
  }
}

async function runContent({
  path = '/', hash = '', search = '', storedJob = null, searchResult = null,
  beginError = false, currentPageState = 'ok', visibleCandidates = null, browserOnly = false,
} = {}) {
  const messages = [];
  const elements = [];
  const assignments = [];
  const historyCalls = [];
  const document = {
    body: new Element('body'),
    createElement(tag) { const el = new Element(tag); elements.push(el); return el; },
  };
  const location = {
    pathname: path,
    search,
    hash,
    href: `https://kad.arbitr.ru${path}${search}${hash}`,
    assign(url) { assignments.push(url); },
  };
  const extensionApi = { runtime: { async sendMessage(message) {
    messages.push(message);
    if (message.type === 'begin') {
      if (beginError) return { error: 'worker_error' };
      return { job: { requestId: message.requestId, caseNumber: message.caseNumber, phase: 'searching', startedAt: 1 } };
    }
    if (message.type === 'get') return { job: storedJob };
    if (message.type === 'card') return { job: { ...storedJob, phase: 'card', cardUrl: message.cardUrl } };
    if (message.type === 'candidates') return { job: { ...storedJob, phase: 'documents', candidates: message.candidates } };
    if (message.type === 'select' || message.type === 'stop') return { job: storedJob };
    return { error: 'unexpected_message' };
  } } };
  const automation = {
    async runSearch() { return searchResult; },
    async runCard() { return { candidates: [{ href: PDF, label: 'Synthetic PDF.pdf' }] }; },
    inspectPage() { return { state: currentPageState }; },
    collectPdfCandidates() { return visibleCandidates ?? [{ href: PDF, label: 'Synthetic PDF.pdf' }]; },
  };
  const sandbox = {
    location,
    document,
    history: { replaceState(...args) { historyCalls.push(args); } },
    URLSearchParams,
    AbortController,
    SroKadAutomation: automation,
  };
  sandbox[browserOnly ? 'browser' : 'chrome'] = extensionApi;
  const context = vm.createContext(sandbox);
  vm.runInContext(await readFile(CONTENT_URL, 'utf8'), context, { filename: 'content.js' });
  for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve));
  return { messages, elements, assignments, historyCalls, document, location, context };
}

const launchHash = `#sro_robot=1&case=${encodeURIComponent(CASE)}&run=${RUN}`;
const docJob = { requestId: RUN, caseNumber: CASE, phase: 'documents', cardUrl: CARD, candidates: [{ url: PDF, label: 'Synthetic PDF.pdf' }] };

test('fresh matched search records card then navigates exactly once', async () => {
  const h = await runContent({ hash: launchHash, searchResult: { state: 'matched', links: [{ href: CARD }] }, browserOnly: true });
  assert.deepEqual(h.messages.map(message => message.type), ['begin', 'card']);
  assert.equal(h.messages[1].cardUrl, CARD);
  assert.deepEqual(h.assignments, [CARD]);
  assert.equal(h.historyCalls.length, 1);
  assert.equal(h.context.chrome, undefined);
});

test('no results and ambiguous results stop without navigation', async t => {
  for (const state of ['no-results', 'ambiguous']) await t.test(state, async () => {
    const h = await runContent({ hash: launchHash, searchResult: { state, links: [] } });
    assert.deepEqual(h.messages.map(message => message.type), ['begin', 'stop']);
    assert.deepEqual(h.assignments, []);
  });
});

test('worker error stops before search or navigation', async () => {
  const h = await runContent({ hash: launchHash, beginError: true, searchResult: { state: 'matched', links: [{ href: CARD }] } });
  assert.deepEqual(h.messages.map(message => message.type), ['begin']);
  assert.deepEqual(h.assignments, []);
  const status = h.elements.find(element => element.attributes.role === 'status');
  assert.match(status.textContent, /Помощник остановлен/);
});

test('document link click revalidates current page and candidate, then records explicit selection', async () => {
  const h = await runContent({
    path: `/Card/${CARD_ID}`,
    storedJob: docJob,
    currentPageState: 'ok',
    visibleCandidates: [{ href: PDF, label: 'Synthetic PDF.pdf' }],
  });
  assert.deepEqual(h.messages.map(message => message.type), ['get']);
  const link = h.elements.find(element => element.tagName === 'a');
  assert.ok(link);
  assert.equal(link.href, PDF);
  assert.equal(link.target, '_blank');
  assert.equal(link.rel, 'noopener noreferrer');
  assert.deepEqual(link.click(), { prevented: false });
  for (let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(h.messages.map(message => message.type), ['get', 'select']);
  assert.equal(h.messages[1].url, PDF);
});

test('changed page or stale candidate prevents document opening and stops the job', async () => {
  const h = await runContent({
    path: `/Card/${CARD_ID}`,
    storedJob: docJob,
    currentPageState: 'ok',
    visibleCandidates: [],
  });
  const link = h.elements.find(element => element.tagName === 'a');
  assert.deepEqual(link.click(), { prevented: true });
  for (let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(h.messages.map(message => message.type), ['get', 'stop']);
});

test('resumed searching state does not repeat search or navigate', async () => {
  const searchingJob = { requestId: RUN, caseNumber: CASE, phase: 'searching', startedAt: 1 };
  const h = await runContent({ storedJob: searchingJob, searchResult: { state: 'matched', links: [{ href: CARD }] } });
  assert.deepEqual(h.messages.map(message => message.type), ['get']);
  assert.deepEqual(h.assignments, []);
  const status = h.elements.find(element => element.attributes.role === 'status');
  assert.match(status.textContent, /Предыдущий запуск завершён или прерван/);
});
