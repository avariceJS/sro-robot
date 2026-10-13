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
  exerciseSleep = false, beginError = false, currentPageState = 'ok', visibleCandidates = null, browserOnly = false, selectError = false, afterSelect = () => {},
  searchError = null, cardError = null,
} = {}) {
  const messages = [];
  const elements = [];
  const assignments = [];
  const reloads = [];
  const historyCalls = [];
  let current = storedJob ? {...storedJob} : null;
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
    reload() { reloads.push(location.href); },
  };
  const extensionApi = { runtime: { getManifest: () => ({version: '0.2.6'}), async sendMessage(message) {
    messages.push(message);
    if (message.type === 'begin') {
      if (beginError) return { error: 'worker_error' };
      current = { requestId: message.requestId, caseNumber: message.caseNumber, phase: 'searching', startedAt: 1, gatewayReloads: 0 };
      return { job: current };
    }
    if (message.type === 'get') return { job: current };
    if (message.type === 'card') {
      current = { ...current, phase: 'card', cardUrl: message.cardUrl, gatewayReloads: 0, gatewayResume: false };
      return { job: current };
    }
    if (message.type === 'candidates') return { job: { ...current, phase: 'documents', candidates: message.candidates } };
    if (message.type === 'select') { afterSelect(); return selectError ? {error: 'selection_error'} : {job: current}; }
    if (message.type === 'acquire') return {job: {...current, phase: 'ready'}};
    if (message.type === 'tick') return {ok: true};
    if (message.type === 'stop') return { job: current };
    if (message.type === 'gateway-reload') {
      const attempts = current?.gatewayReloads || 0;
      if (attempts >= 2) return { error: 'limit', code: 'gateway-unavailable' };
      current = {...current, gatewayReloads: attempts + 1, gatewayResume: true};
      return { job: current, delayMs: attempts === 0 ? 2000 : 6000, reloadLimit: 2 };
    }
    if (message.type === 'gateway-resumed') {
      current = {...current, gatewayResume: false};
      return { job: current };
    }
    return { error: 'unexpected_message' };
  } } };
  const automation = {
    async runSearch(doc, number, options) {
      if (exerciseSleep) await options.sleep(250);
      if (searchError) throw Object.assign(new Error(searchError), {code: searchError});
      return searchResult;
    },
    async runCard(doc, number, options) {
      if (exerciseSleep) await options.sleep(250);
      if (cardError) throw Object.assign(new Error(cardError), {code: cardError});
      return { candidates: [{ href: PDF, label: 'Synthetic PDF.pdf' }] };
    },
    inspectPage() { return { state: currentPageState }; },
    chronologySections() { return [{expanded: true, items: {}}]; },
    collectPdfCandidates() { return typeof visibleCandidates === 'function' ? visibleCandidates() : visibleCandidates ?? [{ href: PDF, label: 'Synthetic PDF.pdf' }]; },
  };
  const sandbox = {
    location,
    document,
    history: { replaceState(state, title, next) {
      historyCalls.push([state, title, next]);
      if (typeof next === 'string') {
        const url = new URL(next, 'https://kad.arbitr.ru');
        location.pathname = url.pathname;
        location.search = url.search;
        location.hash = url.hash;
        location.href = url.href;
      }
    } },
    URLSearchParams,
    AbortController,
    SroKadAutomation: automation,
  };
  sandbox[browserOnly ? 'browser' : 'chrome'] = extensionApi;
  const context = vm.createContext(sandbox);
  vm.runInContext(await readFile(new URL('../browser-extension/kad-helper/protocol.js', import.meta.url), 'utf8'), context, {filename: 'protocol.js'});
  vm.runInContext(await readFile(CONTENT_URL, 'utf8'), context, { filename: 'content.js' });
  for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve));
  return { messages, elements, assignments, reloads, historyCalls, document, location, context };
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

test('a single PDF opens automatically in the same tab after metadata is recorded', async () => {
  const h = await runContent({path: `/Card/${CARD_ID}`, storedJob: docJob});
  assert.deepEqual(h.messages.map(m => m.type), ['get', 'select']);
  assert.equal(h.messages[1].automatic, true);
  assert.equal(h.messages[1].url, PDF);
  assert.deepEqual(h.assignments, [PDF]);
  assert.ok(!h.elements.some(el => /Сохраните PDF|Выберите нужный/.test(el.textContent)));
});

test('fresh card completes acquisition and opens the single PDF', async () => {
  const h = await runContent({path: `/Card/${CARD_ID}`, storedJob: {...docJob, phase: 'card'}});
  assert.deepEqual(h.messages.map(m => m.type), ['get', 'candidates', 'select']);
  assert.deepEqual(h.assignments, [PDF]);
});

test('stale links, access challenges, and failed metadata writes prevent opening', async () => {
  for (const options of [{visibleCandidates: []}, {currentPageState: 'challenge'}, {selectError: true}]) {
    const h = await runContent({path: `/Card/${CARD_ID}`, storedJob: docJob, ...options});
    assert.deepEqual(h.assignments, []);
    assert.equal(h.messages.at(-1).type, 'stop');
  }
});

test('inventory changed during selection prevents automatic navigation', async () => {
  let changed = false;
  const h = await runContent({path: `/Card/${CARD_ID}`, storedJob: docJob,
    afterSelect: () => { changed = true; },
    visibleCandidates: () => changed ? [{href: PDF}, {href: PDF + 'other'}] : [{href: PDF}],
  });
  assert.deepEqual(h.assignments, []);
  assert.equal(h.messages.at(-1).type, 'stop');
});

test('multiple PDFs require a selection and selected runs never auto-reopen', async () => {
  const second = {...docJob.candidates[0], url: PDF.replace('decision.pdf', 'other.pdf')};
  const h = await runContent({path: `/Card/${CARD_ID}`, storedJob: {...docJob, candidates: [...docJob.candidates, second]}});
  assert.deepEqual(h.assignments, []);
  const link = h.elements.find(el => el.tagName === 'a');
  assert.deepEqual(link.click(), {prevented: true});
  for (let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.messages[1].automatic, false);
  assert.deepEqual(h.assignments, [PDF]);
  const resumed = await runContent({path: `/Card/${CARD_ID}`, storedJob: {...docJob, selectedUrl: PDF}});
  assert.deepEqual(resumed.assignments, []);
  assert.deepEqual(resumed.messages.map(m => m.type), ['get']);
});

test('resumed searching state does not repeat search or navigate', async () => {
  const searchingJob = { requestId: RUN, caseNumber: CASE, phase: 'searching', startedAt: 1 };
  const h = await runContent({ storedJob: searchingJob, searchResult: { state: 'matched', links: [{ href: CARD }] } });
  assert.deepEqual(h.messages.map(message => message.type), ['get']);
  assert.deepEqual(h.assignments, []);
  assert.deepEqual(h.reloads, []);
  const status = h.elements.find(element => element.attributes.role === 'status');
  assert.match(status.textContent, /Предыдущий запуск завершён или прерван/);
});

test('a gateway error reloads before search and only the helper resume flag continues it', async () => {
  const failed = await runContent({ hash: launchHash, searchError: 'gateway-unavailable' });
  assert.deepEqual(failed.reloads, ['https://kad.arbitr.ru/']);
  assert.deepEqual(failed.assignments, []);
  assert.equal(failed.messages.filter(message => message.type === 'tick').length, 8);
  assert.equal(failed.messages.some(message => message.type === 'stop'), false);
  assert.equal(failed.messages.find(message => message.type === 'gateway-reload').requestId, RUN);
  const status = failed.elements.find(element => element.attributes.role === 'status');
  assert.match(status.textContent, /попытка 1 из 2/);

  const resumed = await runContent({
    storedJob: { requestId: RUN, caseNumber: CASE, phase: 'searching', gatewayResume: true, gatewayReloads: 1, startedAt: 1 },
    searchResult: { state: 'matched', links: [{ href: CARD }] },
  });
  assert.deepEqual(resumed.messages.map(message => message.type), ['get', 'gateway-resumed', 'card']);
  assert.deepEqual(resumed.assignments, [CARD]);
  assert.deepEqual(resumed.reloads, []);

  const exhausted = await runContent({
    storedJob: { requestId: RUN, caseNumber: CASE, phase: 'searching', gatewayResume: true, gatewayReloads: 2, startedAt: 1 },
    searchError: 'gateway-unavailable',
  });
  assert.deepEqual(exhausted.reloads, []);
  assert.equal(exhausted.messages.at(-1).type, 'stop');
  assert.equal(exhausted.messages.at(-1).reason, 'gateway-unavailable');

  const limited = await runContent({ hash: launchHash, searchError: 'rate-limited' });
  assert.deepEqual(limited.reloads, []);
  assert.equal(limited.messages.at(-1).reason, 'rate-limited');
  const challenged = await runContent({ hash: launchHash, searchError: 'challenge' });
  assert.deepEqual(challenged.reloads, []);
  assert.equal(challenged.messages.at(-1).reason, 'challenge');
});

test('a gateway error on the card reloads that card and does not open a PDF', async () => {
  const cardJob = { requestId: RUN, caseNumber: CASE, phase: 'card', cardUrl: CARD, gatewayResume: true, startedAt: 1 };
  const recovered = await runContent({ path: `/Card/${CARD_ID}`, storedJob: {...cardJob, gatewayResume: false}, cardError: 'gateway-unavailable' });
  assert.deepEqual(recovered.reloads, [CARD]);
  assert.deepEqual(recovered.assignments, []);
  assert.equal(recovered.messages.filter(message => message.type === 'tick').length, 8);
  assert.equal(recovered.messages.some(message => message.type === 'stop'), false);

  const resumed = await runContent({ path: `/Card/${CARD_ID}`, storedJob: cardJob });
  assert.deepEqual(resumed.messages.map(message => message.type), ['get', 'gateway-resumed', 'candidates', 'select']);
  assert.deepEqual(resumed.assignments, [PDF]);
});


test('service acquisition preserves ordinary viewer mode and transfers the sole PDF instead', async () => {
  const h = await runContent({path: `/Card/${CARD_ID}`, storedJob: {...docJob, bridge: {serviceTab: 9}}});
  assert.deepEqual(h.messages.map(m => m.type), ['get', 'select', 'acquire']);
  assert.deepEqual(h.assignments, []);
});

test('service acquisition does not select a random PDF when multiple documents exist', async () => {
  const h = await runContent({path: `/Card/${CARD_ID}`, storedJob: {...docJob, bridge: {serviceTab: 9}, candidates: [...docJob.candidates, {...docJob.candidates[0], url: PDF + 'other'}]}});
  assert.deepEqual(h.messages.map(m => m.type), ['get', 'stop']);
  assert.equal(h.messages[1].reason, 'ambiguous_documents');
  assert.deepEqual(h.assignments, []);
});


test('search and chronology use background ticks without content-page timers', async () => {
  const search = await runContent({hash: launchHash, exerciseSleep: true, searchResult: {state: 'matched', links: [{href: CARD}]}});
  assert.deepEqual(search.messages.map(m => m.type), ['begin', 'tick', 'card']);
  const card = await runContent({path: `/Card/${CARD_ID}`, exerciseSleep: true, storedJob: {...docJob, phase: 'card'}});
  assert.deepEqual(card.messages.map(m => m.type), ['get', 'tick', 'candidates', 'select']);
});
