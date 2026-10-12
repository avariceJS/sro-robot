import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const CASE = 'А99-12345/2099';
const RUN = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
const CARD_ID = '11111111-2222-3333-4444-555555555555';
const OTHER_CARD_ID = '66666666-7777-8888-9999-000000000000';
const CARD = `https://kad.arbitr.ru/Card/${CARD_ID}`;
const PDF = `https://kad.arbitr.ru/Kad/PdfDocument/${CARD_ID}/12345678-1234-1234-1234-123456789012/decision.pdf`;
const OTHER_PDF = `https://kad.arbitr.ru/Kad/PdfDocument/${CARD_ID}/aaaaaaaa-1234-1234-1234-123456789012/order.pdf`;
const NON_UUID_PDF = `https://kad.arbitr.ru/Kad/PdfDocument/${CARD_ID}/document-id/ruling.pdf`;
const OTHER_CARD_PDF = `https://kad.arbitr.ru/Kad/PdfDocument/${OTHER_CARD_ID}/12345678-1234-1234-1234-123456789012/decision.pdf`;
const ROOT = new URL('../browser-extension/kad-helper/', import.meta.url);

async function harness({browserOnly = false, withImportScripts = true} = {}) {
  const stored = new Map();
  let listener;
  const removedListeners = [];
  const protocolSource = await readFile(new URL('protocol.js', ROOT), 'utf8');
  const extensionApi = {
    runtime: {
      id: 'synthetic-extension-id',
      onMessage: { addListener(fn) { listener = fn; } },
    },
    storage: { session: {
      async get(key) { return { [key]: stored.get(key) }; },
      async set(values) { for (const [key, value] of Object.entries(values)) stored.set(key, value); },
      async remove(key) { stored.delete(key); },
    } },
    tabs: { onRemoved: { addListener(fn) { removedListeners.push(fn); } } },
  };
  const sandbox = {
    URL,
    URLSearchParams,
    Date,
    Map,
    Promise,
    Error,
    Number,
    Array,
    String,
    RegExp,
    Object,
  };
  sandbox[browserOnly ? 'browser' : 'chrome'] = extensionApi;
  const context = vm.createContext(sandbox);
  if (withImportScripts) context.importScripts = file => {
    if (file !== 'protocol.js') throw new Error('unexpected_import');
    vm.runInContext(protocolSource, context, { filename: file });
  };
  else vm.runInContext(protocolSource, context, { filename: 'protocol.js' });
  const backgroundSource = await readFile(new URL('background.js', ROOT), 'utf8');
  vm.runInContext(backgroundSource, context, { filename: 'background.js' });
  assert.equal(typeof listener, 'function', 'background must register a message listener');

  const sender = (tabId = 1, overrides = {}) => ({
    id: extensionApi.runtime.id,
    tab: { id: tabId },
    frameId: 0,
    url: 'https://kad.arbitr.ru/',
    ...overrides,
  });
  const send = (message, from = sender()) => new Promise(resolve => {
    const keepChannelOpen = listener(message, from, resolve);
    assert.equal(keepChannelOpen, true);
  });
  return { context, extensionApi, stored, removedListeners, send, sender };
}

test('protocol normalizes the synthetic case and rejects malicious case and URL inputs', async () => {
  const protocolSource = await readFile(new URL('protocol.js', ROOT), 'utf8');
  const context = vm.createContext({ URL, URLSearchParams });
  vm.runInContext(protocolSource, context);
  const p = context.SroKadProtocol;
  assert.equal(p.normalizeCase(` ${CASE.toLowerCase()} `), CASE);
  assert.equal(p.cardURL(CARD), CARD);
  assert.equal(p.launchURL(CASE, RUN), `https://kad.arbitr.ru/#sro_robot=1&case=${encodeURIComponent(CASE)}&run=${RUN}`);

  for (const value of [
    'A99-12345/2099/extra', 'A99-12345/209', 'A99-12345/2099?x=1',
    'A99-12345/20\n99', 'X A99-12345/2099', 99, null,
  ]) assert.throws(() => p.normalizeCase(value), /invalid_case/);
  for (const value of [
    CARD.replace('https://', 'http://'),
    CARD.replace('kad.arbitr.ru', 'kad.arbitr.ru.evil.test'),
    CARD.replace('https://', 'https://user@'),
    `${CARD}?redirect=https://evil.test`, `${CARD}#fragment`, `${CARD}/extra`,
    'https://kad.arbitr.ru\\@evil.test/Card/' + CARD_ID,
    'https://kad.arbitr.ru/Kad/PdfDocument/' + CARD_ID + '/x.pdf?download=1',
  ]) assert.throws(() => p.cardURL(value), undefined, value);
});

test('sender must be this extension, a top-level tab, and on the exact KAD origin', async () => {
  const h = await harness();
  const invalidSenders = [
    h.sender(1, { id: 'other-extension' }),
    h.sender(1, { tab: {} }),
    h.sender(1, { tab: { id: '1' } }),
    h.sender(1, { frameId: 2 }),
    h.sender(1, { url: 'https://kad.arbitr.ru.evil.test/' }),
    h.sender(1, { url: 'https://user@kad.arbitr.ru/' }),
    h.sender(1, { url: 'file:///tmp/kad.html' }),
  ];
  for (const from of invalidSenders) {
    const result = await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE }, from);
    assert.equal(typeof result.error, 'string');
  }
  assert.equal(h.stored.size, 0);
});

test('valid job advances search → card → documents → selected candidate on the same card', async () => {
  const h = await harness();
  let result = await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE });
  assert.equal(result.job.phase, 'searching');
  assert.equal(result.job.caseNumber, CASE);

  result = await h.send({ type: 'card', requestId: RUN, cardUrl: CARD });
  assert.equal(result.job.phase, 'card');
  assert.equal(result.job.cardUrl, CARD);

  result = await h.send({ type: 'candidates', requestId: RUN, candidates: [
    { url: PDF, label: 'Синтетический акт.pdf' },
    { url: OTHER_PDF, label: 'Синтетическое определение.pdf' },
  ] }, h.sender(1, { url: CARD }));
  assert.equal(result.job.phase, 'documents');
  assert.equal(result.job.candidates.length, 2);

  result = await h.send({ type: 'select', requestId: RUN, url: OTHER_PDF }, h.sender(1, { url: CARD }));
  assert.equal(result.job.selectedUrl, OTHER_PDF);
  assert.equal(result.job.selectionReason, 'employee_selected_visible_document');
});

test('missing, mismatched, and expired job messages fail closed', async () => {
  const h = await harness();
  assert.deepEqual((await h.send({ type: 'get' })).job, null);
  assert.equal(typeof (await h.send({ type: 'card', requestId: RUN, cardUrl: CARD })).error, 'string');

  await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE });
  assert.equal(typeof (await h.send({ type: 'card', requestId: OTHER_CARD_ID, cardUrl: CARD })).error, 'string');

  h.stored.set('kad:1', { requestId: RUN, caseNumber: CASE, phase: 'searching', startedAt: Date.now() - 31 * 60 * 1000 });
  const expired = await h.send({ type: 'card', requestId: RUN, cardUrl: CARD });
  assert.equal(typeof expired.error, 'string');
  assert.equal(h.stored.has('kad:1'), false);
});

test('replaying the same begin request is idempotent and starts no replacement search', async () => {
  const h = await harness();
  const first = await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE });
  const replay = await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE });
  assert.equal(replay.replay, true);
  assert.equal(replay.job.startedAt, first.job.startedAt);
  assert.equal(h.stored.get('kad:1').startedAt, first.job.startedAt);
});

test('Firefox MV3 background scripts work without importScripts and with browser namespace only', async () => {
  const h = await harness({browserOnly: true, withImportScripts: false});
  assert.equal(h.context.chrome, undefined);
  const started = await h.send({type: 'begin', requestId: RUN, caseNumber: CASE});
  assert.equal(started.job.phase, 'searching');
  const card = await h.send({type: 'card', requestId: RUN, cardUrl: CARD});
  assert.equal(card.job.cardUrl, CARD);
  assert.equal(h.stored.get('kad:1').phase, 'card');
});

test('extension manifest declares compatible dual MV3 background formats without new permissions', async () => {
  const manifest = JSON.parse(await readFile(new URL('manifest.json', ROOT), 'utf8'));
  assert.equal(manifest.version, '0.1.5');
  assert.equal(manifest.minimum_chrome_version, '121');
  assert.equal(manifest.browser_specific_settings.gecko.strict_min_version, '128');
  assert.deepEqual(manifest.background.scripts, ['protocol.js', 'background.js']);
  assert.equal(manifest.background.service_worker, 'background.js');
  assert.deepEqual(manifest.permissions, ['storage']);
  assert.deepEqual(manifest.host_permissions, ['https://kad.arbitr.ru/*']);
});

test('popup uses the browser namespace to launch KAD without requiring chrome', async () => {
  const protocolSource = await readFile(new URL('protocol.js', ROOT), 'utf8');
  const popupSource = await readFile(new URL('popup.js', ROOT), 'utf8');
  let submit;
  let opened;
  let closed = false;
  let prevented = false;
  const elements = {
    '#search': {addEventListener(_type, listener) { submit = listener; }},
    '#case': {value: CASE},
    '#status': {textContent: ''},
  };
  const browser = {tabs: {async create({url}) { opened = url; }}};
  const context = vm.createContext({
    URL, URLSearchParams,
    browser,
    document: {querySelector(selector) { return elements[selector]; }},
    crypto: {randomUUID: () => RUN},
    window: {close() { closed = true; }},
  });
  vm.runInContext(protocolSource, context, {filename: 'protocol.js'});
  vm.runInContext(popupSource, context, {filename: 'popup.js'});
  await submit({preventDefault() { prevented = true; }});
  assert.equal(context.chrome, undefined);
  assert.equal(prevented, true);
  assert.equal(opened, `https://kad.arbitr.ru/#sro_robot=1&case=${encodeURIComponent(CASE)}&run=${RUN}`);
  assert.equal(closed, true);
});

test('jobs are isolated by tab and concurrent messages for one tab serialize', async () => {
  const h = await harness();
  const [a, b] = await Promise.all([
    h.send({ type: 'begin', requestId: RUN, caseNumber: CASE }, h.sender(1)),
    h.send({ type: 'begin', requestId: 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff', caseNumber: 'А99-12345/2099' }, h.sender(2)),
  ]);
  assert.equal(a.job.requestId, RUN);
  assert.equal(b.job.requestId, 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff');
  assert.notEqual(h.stored.get('kad:1'), h.stored.get('kad:2'));

  const [first, second] = await Promise.all([
    h.send({ type: 'begin', requestId: '11111111-aaaa-bbbb-cccc-222222222222', caseNumber: CASE }, h.sender(3)),
    h.send({ type: 'begin', requestId: '33333333-aaaa-bbbb-cccc-444444444444', caseNumber: CASE }, h.sender(3)),
  ]);
  assert.equal(first.job.requestId, '11111111-aaaa-bbbb-cccc-222222222222');
  assert.equal(second.job.requestId, '33333333-aaaa-bbbb-cccc-444444444444');
  assert.equal(h.stored.get('kad:3').requestId, second.job.requestId);
});

test('candidate list and selection must belong to the current card and candidate set', async () => {
  const h = await harness();
  await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE });
  await h.send({ type: 'card', requestId: RUN, cardUrl: CARD });

  const wrongCard = await h.send({ type: 'candidates', requestId: RUN, candidates: [
    { url: OTHER_CARD_PDF, label: 'Wrong card.pdf' },
  ] }, h.sender(1, { url: CARD }));
  assert.equal(typeof wrongCard.error, 'string');

  const wrongPage = await h.send({ type: 'candidates', requestId: RUN, candidates: [
    { url: PDF, label: 'Valid.pdf' },
  ] }, h.sender(1, { url: `https://kad.arbitr.ru/Card/${OTHER_CARD_ID}` }));
  assert.equal(typeof wrongPage.error, 'string');

  await h.send({ type: 'candidates', requestId: RUN, candidates: [
    { url: PDF, label: 'Valid.pdf' },
  ] }, h.sender(1, { url: CARD }));
  const arbitrary = await h.send({ type: 'select', requestId: RUN, url: OTHER_PDF }, h.sender(1, { url: CARD }));
  assert.equal(typeof arbitrary.error, 'string');
});

test('candidate path must contain card UUID, document UUID, and PDF filename; duplicate URLs fail', async () => {
  const h = await harness();
  await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE });
  await h.send({ type: 'card', requestId: RUN, cardUrl: CARD });
  for (const candidates of [
    [{ url: NON_UUID_PDF, label: 'Non-UUID path.pdf' }],
    [{ url: OTHER_CARD_PDF, label: 'Different card.pdf' }],
    [{ url: PDF, label: 'First.pdf' }, { url: PDF, label: 'Duplicate.pdf' }],
  ]) {
    const result = await h.send({ type: 'candidates', requestId: RUN, candidates }, h.sender(1, { url: CARD }));
    assert.equal(typeof result.error, 'string');
    assert.notEqual(h.stored.get('kad:1').phase, 'documents');
  }
});

test('background uses no direct network API and accepts no arbitrary URL for navigation or fetch', async () => {
  const [background, protocol, automation] = await Promise.all([
    readFile(new URL('background.js', ROOT), 'utf8'),
    readFile(new URL('protocol.js', ROOT), 'utf8'),
    readFile(new URL('automation.js', ROOT), 'utf8'),
  ]);
  for (const source of [background, protocol, automation]) {
    assert.doesNotMatch(source, /\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\b/);
  }
  assert.doesNotMatch(background, /chrome\.tabs\.(?:create|update)\s*\(/);
  assert.doesNotMatch(background, /chrome\.downloads\./);
});
