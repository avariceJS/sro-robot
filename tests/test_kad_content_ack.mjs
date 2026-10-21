import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const CARD = 'https://kad.arbitr.ru/Card/11111111-2222-3333-4444-555555555555';
const PDF = 'https://kad.arbitr.ru/Kad/PdfDocument/11111111-2222-3333-4444-555555555555/12345678-1234-1234-1234-123456789012/decision.pdf';
const RUN = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';

async function deliverSelection({collectCandidates, acquireResult, deferSelect = false} = {}) {
  const listeners = [];
  const messages = [];
  let releaseSelect;
  let controller;
  const candidate = {url: PDF, label: 'Synthetic act', actDate: '2024-02-29'};
  const job = {
    requestId: RUN, phase: 'documents', caseNumber: 'А99-12345/2099', actDate: '2024-02-29',
    cardUrl: CARD, bridge: {serviceTab: 1}, candidateInventoryComplete: false,
    candidates: [candidate],
  };
  const api = {runtime: {
    getManifest: () => ({version: '0.3.0'}),
    onMessage: {addListener(fn) { listeners.push(fn); }},
    async sendMessage(message) {
      messages.push(message.type);
      if (message.type === 'get') return {job};
      if (message.type === 'select') {
        if (deferSelect) return new Promise(resolve => { releaseSelect = () => resolve({ok: true}); });
        return {ok: true};
      }
      if (message.type === 'acquire') {
        if (acquireResult instanceof Error) throw acquireResult;
        return acquireResult;
      }
      return {ok: true};
    },
  }};
  const element = () => ({style: {}, children: [], append(...items) { this.children.push(...items); },
    setAttribute() {}, addEventListener() {}, replaceChildren() { this.children = []; }});
  const doc = {
    body: element(),
    createElement: element,
  };
  const automation = {
    inspectPage: () => ({state: 'ok'}),
    chronologySections: () => [{expanded: true, items: {}}],
    collectPdfCandidates: () => collectCandidates ? collectCandidates(candidate) : [{href: PDF, url: PDF, label: candidate.label, actDate: candidate.actDate}],
  };
  const protocol = {
    automaticDocument: () => ({error: 'incomplete_inventory'}),
    documentSelection: (_items, _date, url) => ({candidate: {url}, reason: 'employee_selected_visible_document'}),
  };
  const location = {pathname: '/Card/11111111-2222-3333-4444-555555555555', href: CARD, hash: '', search: '', assign() {}};
  class CapturedAbortController extends AbortController {
    constructor() { super(); controller = this; }
  }
  const context = vm.createContext({
    URL, URLSearchParams, AbortController: CapturedAbortController, Date, Promise, Error, Object, Array, String, Number, RegExp,
    document: doc, location, history: {replaceState() {}}, browser: api,
    SroKadAutomation: automation, SroKadProtocol: protocol,
  });
  const source = await readFile(new URL('../browser-extension/kad-helper/content.js', import.meta.url), 'utf8');
  vm.runInContext(source, context, {filename: 'content.js'});
  await new Promise(resolve => setTimeout(resolve, 0));
  const listener = listeners.find(fn => typeof fn === 'function');
  assert.equal(typeof listener, 'function');

  const responsePromise = new Promise(resolve => {
    assert.equal(listener({type: 'service-select', requestId: RUN, url: PDF}, {}, resolve), true);
  });
  return {responsePromise, messages, controller, releaseSelect: () => releaseSelect?.()};
}

test('service selection is acknowledged after revalidation and before PDF navigation', async () => {
  const {responsePromise, messages} = await deliverSelection({acquireResult: new Error('message port closed by PDF navigation')});
  const response = await responsePromise;
  assert.equal(response.ok, true);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(messages.slice(-3), ['get', 'select', 'acquire']);
  assert.equal(messages.includes('stop'), false, 'expected navigation channel closure must not cancel the background capture');
});

test('service acknowledgment does not wait for the native PDF capture response', async () => {
  const pending = await deliverSelection({acquireResult: new Promise(() => {})});
  const response = await pending.responsePromise;
  assert.equal(response.ok, true);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(pending.messages.includes('acquire'), true);
  assert.equal(pending.messages.includes('stop'), false);
});

test('changed document inventory is rejected before acquisition', async () => {
  let reads = 0;
  const {responsePromise, messages} = await deliverSelection({collectCandidates: candidate => {
    reads++;
    return reads === 1 ? [{href: PDF, url: PDF, label: candidate.label, actDate: candidate.actDate}] : [];
  }});
  const response = await responsePromise;
  assert.equal(response.error, 'document_changed');
  assert.equal(messages.includes('acquire'), false);
  assert.equal(messages.includes('stop'), true);
});

test('abort while persisting the selection replies with failure and never acquires', async () => {
  const pending = await deliverSelection({deferSelect: true});
  while (!pending.releaseSelect) await new Promise(resolve => setTimeout(resolve, 0));
  pending.controller.abort();
  pending.releaseSelect();
  const response = await pending.responsePromise;
  assert.equal(response.error, 'aborted');
  assert.equal(pending.messages.includes('acquire'), false);
});
