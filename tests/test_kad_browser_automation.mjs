import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const kad = require('../browser-extension/kad-helper/automation.js');
const CASE = 'А99-12345/2099';
const CARD = 'https://kad.arbitr.ru/Card/11111111-2222-3333-4444-555555555555';
const CARD_ID = '11111111-2222-3333-4444-555555555555';
const DOC_ID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
const pdfUrl = (cardId = CARD_ID, docId = DOC_ID, file = 'act.pdf') =>
  `https://kad.arbitr.ru/Kad/PdfDocument/${cardId}/${docId}/${file}`;

class Element {
  constructor(text = '', attrs = {}) {
    this.innerText = text;
    this.textContent = text;
    this.attrs = attrs;
    this.children = [];
    this.value = '';
    this.hidden = false;
    this.ownerDocument = null;
    this.clicks = 0;
    this.handlers = {};
  }
  getAttribute(name) { return this.attrs[name] ?? null; }
  get id() { return this.attrs.id || ''; }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener(name, fn) { (this.handlers[name] ||= []).push(fn); }
  removeEventListener(name, fn) { this.handlers[name] = (this.handlers[name] || []).filter(x => x !== fn); }
  dispatchEvent(event) { for (const fn of this.handlers[event.type] || []) fn(event); return true; }
  click() { this.clicks++; this.attrs.onClick?.(); }
  getBoundingClientRect() { return { width: 20, height: 10 }; }
  querySelectorAll(selector) { return this.attrs.query?.[selector] || []; }
  cloneNode() {
    const clone = new Element(this.innerText);
    clone.removedHelper = this.attrs.helperText || '';
    clone.querySelector = selector => selector === '#sro-kad-helper' ? { remove: () => { clone.innerText = clone.innerText.replace(clone.removedHelper, ''); } } : null;
    return clone;
  }
}

function makeView() {
  const listeners = {};
  return {
    HTMLInputElement: class HTMLInputElement extends Element {},
    Event: class { constructor(type, options) { this.type = type; Object.assign(this, options); } },
    NodeFilter: { SHOW_TEXT: 4 },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }),
    addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    removeEventListener(name, fn) { listeners[name] = (listeners[name] || []).filter(x => x !== fn); },
    dispatch(name, event) { for (const fn of listeners[name] || []) fn(event); },
  };
}
function makeDoc({ href = 'https://kad.arbitr.ru/', title = '', bodyText = '', selectors = {} } = {}) {
  const view = makeView();
  view.performance = { getEntriesByType: () => [] };
  const body = new Element(bodyText);
  body.ownerDocument = null;
  const doc = {
    defaultView: view,
    location: { href },
    URL: href,
    readyState: 'complete',
    scripts: [],
    title,
    body,
    querySelector: selector => selectors[selector] || null,
    querySelectorAll: selector => selectors[`all:${selector}`] || [],
    createTreeWalker(root, _whatToShow) {
      const entries = root.attrs.textEntries || [{ nodeValue: root.innerText || '', parentElement: root }];
      let index = 0;
      return { nextNode: () => entries[index++] || null };
    },
  };
  body.ownerDocument = doc;
  return doc;
}
function row(doc, text, anchors = []) {
  const r = new Element(text, { query: { a: anchors } });
  r.ownerDocument = doc;
  for (const a of anchors) a.ownerDocument = doc;
  return r;
}
function link(doc, text, href, attrs = {}) {
  const a = new Element(text, attrs);
  a.href = href;
  a.ownerDocument = doc;
  return a;
}
function quickOptions() { let time = 0; return { timeoutMs: 12, pollIntervalMs: 1, settleMs: 0, now: () => time, sleep: async ms => { time += ms; } }; }

test('case normalization and exact public card URL validation are strict', () => {
  assert.equal(kad.normalizeCase(' A99-12345/2099 '), CASE);
  assert.equal(kad.normalizeCase('а99-12345/2099'), CASE);
  assert.deepEqual(kad.validateCardUrl(CARD), { href: CARD, cardId: '11111111-2222-3333-4444-555555555555' });
  for (const bad of [
    'http://kad.arbitr.ru/Card/11111111-2222-3333-4444-555555555555',
    'https://kad.arbitr.ru.evil/Card/11111111-2222-3333-4444-555555555555',
    'https://user@kad.arbitr.ru/Card/11111111-2222-3333-4444-555555555555',
    'https://@kad.arbitr.ru/Card/11111111-2222-3333-4444-555555555555',
    `${CARD}?x=1`, `${CARD}#section`, `${CARD};x`, `${CARD} `,
  ]) assert.equal(kad.validateCardUrl(bad), null, bad);
});

test('exact link collection rejects embedded and truncated case tokens', () => {
  const doc = makeDoc();
  const exact = row(doc, `${CASE} · Арбитражный суд`, [link(doc, CASE, CARD)]);
  const embedded = row(doc, `X${CASE}`, [link(doc, CASE, CARD)]);
  const suffixed = row(doc, `${CASE}/7`, [link(doc, CASE, CARD)]);
  doc.querySelectorAll = selector => selector === 'tr, [role=row]' ? [exact, embedded, suffixed] : [];
  const found = kad.collectExactCaseLinks(doc, CASE);
  assert.equal(found.exactRowIds.length, 1);
  assert.equal(found.links.length, 1);
  assert.equal(found.links[0].href, CARD);
});

test('freshness accepts only a new exact matching row, not unrelated row churn', () => {
  const doc = makeDoc();
  const old = row(doc, CASE, [link(doc, CASE, CARD)]);
  doc.querySelectorAll = selector => selector === 'tr, [role=row]' ? [old] : [];
  const before = kad.snapshot(doc);
  const unrelated = row(doc, 'А99-12345/2098', []);
  doc.querySelectorAll = selector => selector === 'tr, [role=row]' ? [old, unrelated] : [];
  const after = kad.snapshot(doc);
  assert.deepEqual(kad.exactFreshIds(before, after, CASE), []);
  const replacement = row(doc, CASE, [link(doc, CASE, CARD)]);
  doc.querySelectorAll = selector => selector === 'tr, [role=row]' ? [replacement] : [];
  assert.equal(kad.exactFreshIds(before, kad.snapshot(doc), CASE).length, 1);
});

function searchDoc({ clickBehavior, initialCount = null, initialRow = null, prompt = true, commitFilter = true } = {}) {
  let currentRows = initialRow ? [initialRow] : [];
  let bodyText = prompt ? 'Укажите атрибуты дела для поиска' : '';
  if (initialCount !== null) bodyText += ` Найдено ${initialCount} дел`;
  const doc = makeDoc({ bodyText });
  doc._rows = currentRows;
  const input = new doc.defaultView.HTMLInputElement();
  delete input.value;
  input.ownerDocument = doc;
  const nativeValue = Object.getOwnPropertyDescriptor(doc.defaultView.HTMLInputElement.prototype, 'value') || {
    set(v) { this._value = v; }, get() { return this._value; },
  };
  Object.defineProperty(doc.defaultView.HTMLInputElement.prototype, 'value', nativeValue);
  const filterInput = new Element(); filterInput.value = '';
  let added = null;
  const filter = new Element();
  filter.querySelectorAll = selector => selector === '.tag.added' ? (added ? [added] : []) : selector === 'input' ? [filterInput] : [];
  const add = new Element();
  add.click = () => {
    add.clicks++;
    if (commitFilter) {
      filterInput.value = input.value;
      added = new Element('tag', { query: { input: [filterInput] } });
      added.ownerDocument = doc;
      filterInput.ownerDocument = doc;
      input.value = '';
    }
  };
  const button = new Element("Найти");
  button.tagName = "BUTTON";
  button.click = () => {
    button.clicks++;
    clickBehavior?.({ doc, setRows: rows => { currentRows = rows; doc._rows = rows; }, setBody: text => { doc.body.innerText = text; }, input, committedCase: filterInput.value });
  };
  doc.querySelector = selector => ({
    'input[placeholder="например, А50-5568/08"]': input,
    '#sug-cases .tag > i.b-icon.add': add,
    '#sug-cases': filter,
    '#b-form-submit': button,
  })[selector] || null;
  doc.querySelectorAll = selector => selector === 'tr, [role=row]' ? currentRows
    : selector === '#b-form-submit button[type="submit"]' ? [button] : [];
  return { doc, input, filterInput, get added() { return added; }, add, button };
}

test('runSearch commits the input as an active filter before returning a fresh unique exact link', async () => {
  const fixture = searchDoc({ clickBehavior: ({ doc, setRows, committedCase }) => {
    const found = row(doc, `${committedCase} · суд`, [link(doc, committedCase, CARD)]);
    setRows([found]);
    doc.body.innerText = 'Найдено 1 дело';
  } });
  const events = [];
  fixture.input.addEventListener('input', () => events.push('input'));
  fixture.input.addEventListener('change', () => events.push('change'));
  const result = await kad.runSearch(fixture.doc, CASE, quickOptions());
  assert.equal(result.state, 'matched');
  assert.equal(result.links.length, 1);
  assert.deepEqual(events, ['input', 'change']);
  assert.equal(fixture.button.clicks, 1);
  assert.equal(fixture.add.clicks, 1);
  assert.equal(fixture.filterInput.value, CASE);
  assert.equal(fixture.input.value, '');
  assert.notEqual(fixture.input.value, fixture.filterInput.value);
  assert.ok(fixture.added);
});

test('runSearch waits for initial controls and uses only the exact visible Найти fallback', async () => {
  const fixture = searchDoc({ clickBehavior: ({ doc, setRows, committedCase }) => {
    setRows([row(doc, committedCase, [link(doc, committedCase, CARD)])]);
    doc.body.innerText = 'Найдено 1 дело';
  } });
  const originalQuery = fixture.doc.querySelector;
  let delayedLookups = 0;
  fixture.doc.querySelector = selector => {
    if (selector === 'input[placeholder="например, А50-5568/08"]' || selector === '#sug-cases .tag > i.b-icon.add') {
      delayedLookups++;
      if (delayedLookups <= 2) return null;
    }
    if (selector === '#b-form-submit') return null;
    return originalQuery(selector);
  };
  const unrelatedSubmit = new Element('Export');
  fixture.button.innerText = 'Найти';
  fixture.doc.querySelectorAll = selector => selector === 'tr, [role=row]' ? fixture.doc._rows || []
    : selector === 'button[type="submit"]' ? [unrelatedSubmit, fixture.button] : [];
  assert.equal((await kad.runSearch(fixture.doc, CASE, quickOptions())).state, 'matched');
  assert.equal(delayedLookups, 4);
  assert.equal(unrelatedSubmit.clicks, 0);
  assert.equal(fixture.button.clicks, 1);
});

test('runSearch clicks the real submit button instead of its clickable ID wrapper', async () => {
  const fixture = searchDoc({ clickBehavior: ({ doc, setRows, committedCase }) => {
    setRows([row(doc, committedCase, [link(doc, committedCase, CARD)])]);
    doc.body.innerText = 'Найдено 1 дело';
  } });
  const wrapper = new Element('Найти');
  wrapper.tagName = 'DIV';
  const originalQuery = fixture.doc.querySelector;
  fixture.doc.querySelector = selector => selector === '#b-form-submit' ? wrapper : originalQuery(selector);
  fixture.doc.querySelectorAll = selector => selector === 'tr, [role=row]' ? fixture.doc._rows
    : selector === 'button[type="submit"]' ? [fixture.button] : [];
  assert.equal((await kad.runSearch(fixture.doc, CASE, quickOptions())).state, 'matched');
  assert.equal(wrapper.clicks, 0);
  assert.equal(fixture.button.clicks, 1);
});

test('runSearch reports readiness, input, committed filter, button, and submit in order', async () => {
  const fixture = searchDoc({ clickBehavior: ({ doc, setRows, committedCase }) => {
    setRows([row(doc, committedCase, [link(doc, committedCase, CARD)])]);
    doc.body.innerText = 'Найдено 1 дело';
  } });
  const stages = [];
  const result = await kad.runSearch(fixture.doc, CASE, {...quickOptions(), onProgress: stage => stages.push(stage)});
  assert.equal(result.state, 'matched');
  assert.deepEqual(stages, ['ready', 'input', 'filter', 'button', 'submitted']);
  assert.equal(fixture.button.clicks, 1);
  assert.equal(fixture.add.clicks, 1);
});

test('an uncommitted criterion never submits a broad search', async () => {
  const fixture = searchDoc({commitFilter: false});
  await assert.rejects(kad.runSearch(fixture.doc, CASE, quickOptions()), e => e.code === 'search-filter');
  assert.equal(fixture.add.clicks, 1);
  assert.equal(fixture.filterInput.value, '');
  assert.equal(fixture.button.clicks, 0);
});

test('changed committed criterion before submit stops without clicking the search button', async () => {
  const fixture = searchDoc();
  await assert.rejects(kad.runSearch(fixture.doc, CASE, {...quickOptions(),
    onProgress: stage => { if (stage === 'button') fixture.filterInput.value = ''; },
  }), e => e.code === 'search-filter');
  assert.equal(fixture.button.clicks, 0);
});

test('runSearch waits for same-origin scripts to load and settle before entering or clicking', async () => {
  const fixture = searchDoc({clickBehavior: ({doc, setRows, committedCase}) => {
    setRows([row(doc, committedCase, [link(doc, committedCase, CARD)])]);
    doc.body.innerText = 'Найдено 1 дело';
  }});
  const scriptUrl = 'https://kad.arbitr.ru/js/search.js';
  fixture.doc.scripts = [{src: scriptUrl}];
  let time = 0;
  let loaded = false;
  const inputEvents = [];
  fixture.input.addEventListener('input', () => { inputEvents.push(time); });
  fixture.doc.defaultView.performance.getEntriesByType = () => loaded ? [{name: scriptUrl}] : [];
  const stages = [];
  const promise = kad.runSearch(fixture.doc, CASE, {
    timeoutMs: 12, pollIntervalMs: 1, settleMs: 2,
    now: () => time,
    sleep: async ms => { time += ms; if (time >= 3) loaded = true; },
    onProgress: stage => stages.push({stage, time}),
  });
  assert.equal((await promise).state, 'matched');
  assert.ok(inputEvents[0] >= 4, `input fired before script readiness: ${inputEvents[0]}`);
  assert.equal(fixture.add.clicks, 1);
  assert.equal(fixture.button.clicks, 1);
  assert.equal(stages[0].stage, 'ready');
  assert.ok(stages[0].time < inputEvents[0]);
});

test('an unsettled page never receives input or search clicks', async () => {
  const fixture = searchDoc();
  fixture.doc.scripts = [{src: 'https://kad.arbitr.ru/js/search.js'}];
  const events = [];
  fixture.input.addEventListener('input', () => events.push('input'));
  await assert.rejects(kad.runSearch(fixture.doc, CASE, {
    ...quickOptions(), settleMs: 2,
  }), e => e.code === 'page-ready');
  assert.deepEqual(events, []);
  assert.notEqual(fixture.input.value, CASE);
  assert.equal(fixture.add.clicks, 0);
  assert.equal(fixture.button.clicks, 0);
});

test('runSearch aborts before any DOM click and times out if controls never become ready', async () => {
  const aborted = searchDoc();
  const controller = new AbortController(); controller.abort();
  await assert.rejects(kad.runSearch(aborted.doc, CASE, { ...quickOptions(), signal: controller.signal }), e => e.code === 'aborted');
  assert.equal(aborted.add.clicks, 0);
  assert.equal(aborted.button.clicks, 0);

  const missing = searchDoc();
  missing.doc.querySelector = selector => selector === 'input[placeholder="например, А50-5568/08"]' ? null
    : selector === '#sug-cases .tag > i.b-icon.add' ? null : selector === '#sug-cases' ? null : null;
  await assert.rejects(kad.runSearch(missing.doc, CASE, quickOptions()), e => e.code === 'timeout' && /controls did not become ready/.test(e.message));
  assert.equal(missing.button.clicks, 0);
});

test('runSearch refuses cached exact row despite unrelated row and old count', async () => {
  const doc = makeDoc({ bodyText: 'Найдено 1 дело' });
  const cached = row(doc, CASE, [link(doc, CASE, CARD)]);
  doc.querySelectorAll = selector => selector === 'tr, [role=row]' ? [cached] : [];
  const fixture = searchDoc({ initialCount: 1, prompt: false, initialRow: cached,
    clickBehavior: ({ doc, setRows }) => { setRows([cached, row(doc, 'А99-12345/2098')]); doc.body.innerText = 'Найдено 1 дело'; } });
  await assert.rejects(kad.runSearch(fixture.doc, CASE, quickOptions()), e => e.code === 'timeout');
});

test('runSearch reports changed completed zero and fails unchanged timeout distinctly', async () => {
  const zero = searchDoc({ clickBehavior: ({ doc, setRows }) => { setRows([]); doc.body.innerText = 'Найдено 0 дел'; } });
  assert.deepEqual(await kad.runSearch(zero.doc, CASE, quickOptions()), { state: 'no-results', resultCount: 0, links: [] });
  const unchanged = searchDoc({ clickBehavior: () => {} });
  await assert.rejects(kad.runSearch(unchanged.doc, CASE, quickOptions()), e => e.code === 'timeout' && /state did not change/.test(e.message));
});

test('runSearch stops on visible rate limit, explicit WebAssembly failure, and non-unique result count', async () => {
  const limited = searchDoc({ clickBehavior: ({ doc }) => { doc.body.innerText = 'Ошибка 429'; } });
  await assert.rejects(kad.runSearch(limited.doc, CASE, quickOptions()), e => e.code === 'rate-limited');

  const wasm = searchDoc({ clickBehavior: ({ doc }) => {
    doc.defaultView.dispatch('error', { filename: 'wasm_bg.wasm', message: 'runtime error' });
  } });
  await assert.rejects(kad.runSearch(wasm.doc, CASE, quickOptions()), e => e.code === 'wasm-error');

  const ambiguous = searchDoc({ clickBehavior: ({ doc, setRows, committedCase }) => {
    setRows([row(doc, committedCase, [link(doc, committedCase, CARD)])]);
    doc.body.innerText = 'Найдено 2 дела';
  } });
  const result = await kad.runSearch(ambiguous.doc, CASE, quickOptions());
  assert.equal(result.state, 'ambiguous');
  assert.equal(result.resultCount, 2);
  assert.deepEqual(result.links, []);
});

test('challenge and rate-limit are classified from site content, helper overlay ignored', () => {
  const challenge = makeDoc({ bodyText: 'Сайт сообщает: доступ ограничен' });
  assert.equal(kad.inspectPage(challenge).state, 'challenge');
  const limited = makeDoc({ bodyText: 'Ошибка 429' });
  assert.equal(kad.inspectPage(limited).state, 'rate-limited');
  const clean = makeDoc({ bodyText: 'Судебная карточка', selectors: {} });
  clean.body.attrs.helperText = 'CAPTCHA';
  const helper = new Element('', { id: 'sro-kad-helper' }); helper.ownerDocument = clean;
  clean.body.attrs.textEntries = [
    { nodeValue: 'Судебная карточка', parentElement: clean.body },
    { nodeValue: 'CAPTCHA', parentElement: helper },
  ];
  assert.equal(kad.inspectPage(clean).state, 'ok');
});

test('inspectPage rejects userinfo and classifies only rendered visible site text', () => {
  const credentialed = makeDoc({ href: 'https://user@kad.arbitr.ru/' });
  assert.equal(kad.inspectPage(credentialed).state, 'unsafe-navigation');
  const hidden = makeDoc({ bodyText: 'Обычная страница' });
  const hiddenNode = new Element();
  hiddenNode.ownerDocument = hidden;
  hidden.defaultView.getComputedStyle = node => node === hiddenNode
    ? { display: 'none', visibility: 'hidden', opacity: '0' }
    : { display: 'block', visibility: 'visible', opacity: '1' };
  hidden.body.attrs.textEntries = [
    { nodeValue: 'Обычная страница', parentElement: hidden.body },
    { nodeValue: 'CAPTCHA', parentElement: hiddenNode },
  ];
  assert.equal(kad.inspectPage(hidden).state, 'ok');
});

test('script failures are generic unless evidence explicitly identifies WebAssembly', () => {
  const doc = makeDoc();
  assert.equal(kad.inspectPage(doc, ['javascript']).state, 'script-error');
  assert.equal(kad.inspectPage(doc, ['wasm']).state, 'wasm-error');
});

test('runCard verifies synthetic card, clicks exact electronic-case control and returns all PDF candidates', async () => {
  const doc = makeDoc({ href: CARD, title: CASE, bodyText: `${CASE}\nЭлектронное дело` });
  const heading = new Element(`${CASE} · Арбитражный суд`); heading.ownerDocument = doc;
  const pdf1 = link(doc, 'Определение.pdf', pdfUrl());
  const pdf2 = link(doc, 'Акт.pdf', pdfUrl(CARD_ID, '99999999-8888-7777-6666-555555555555', 'act-2.pdf'));
  const nestedLabel = new Element('Электронное дело'); nestedLabel.ownerDocument = doc;
  const control = new Element('Электронное дело', { query: { '*': [nestedLabel] } }); control.ownerDocument = doc;
  nestedLabel.parentElement = control;
  control.click = () => { control.clicks++; };
  doc.querySelectorAll = selector => {
    if (selector === 'h1, h2, h3, h4, [role=heading]') return [heading];
    if (selector === '*') return [control, nestedLabel];
    if (selector === 'button, a, [role=button], [role=tab]') return [control];
    if (selector === 'a') return [pdf1, pdf2];
    return [];
  };
  const result = await kad.runCard(doc, CASE, { ...quickOptions(), cardUrl: CARD });
  assert.equal(result.state, 'candidates');
  assert.equal(result.candidates.length, 2);
  assert.equal(result.selectionRequired, true);
  assert.equal(nestedLabel.clicks, 1);
});

test('runCard waits for card scripts to load and settle before clicking electronic case', async () => {
  const doc = makeDoc({ href: CARD, title: CASE, bodyText: `${CASE}\nЭлектронное дело` });
  const script = 'https://kad.arbitr.ru/js/card.js';
  doc.scripts = [{ src: script }];
  let time = 0;
  let loaded = false;
  doc.defaultView.performance.getEntriesByType = () => loaded ? [{ name: script }] : [];
  const heading = new Element(CASE); heading.ownerDocument = doc;
  const control = new Element('Электронное дело'); control.ownerDocument = doc;
  let clickAt = null;
  control.click = () => { control.clicks++; clickAt = time; };
  const candidate = link(doc, 'Документ.pdf', pdfUrl());
  doc.querySelectorAll = selector => selector === 'h1, h2, h3, h4, [role=heading]' ? [heading]
    : selector === '*' ? [control] : selector === 'a' ? [candidate] : [];

  const result = await kad.runCard(doc, CASE, {
    cardUrl: CARD, timeoutMs: 20, pollIntervalMs: 1, settleMs: 2,
    now: () => time,
    sleep: async ms => { time += ms; if (time >= 3) loaded = true; },
  });
  assert.equal(result.state, 'candidates');
  assert.equal(result.candidates.length, 1);
  assert.ok(clickAt >= 4, `electronic-case control clicked before page readiness: ${clickAt}`);
  assert.equal(control.clicks, 1);
});

test('runCard does not click the electronic-case control when scripts never settle', async () => {
  const doc = makeDoc({ href: CARD, title: CASE, bodyText: `${CASE}\nЭлектронное дело` });
  doc.scripts = [{ src: 'https://kad.arbitr.ru/js/card.js' }];
  const heading = new Element(CASE); heading.ownerDocument = doc;
  const control = new Element('Электронное дело'); control.ownerDocument = doc;
  doc.querySelectorAll = selector => selector === 'h1, h2, h3, h4, [role=heading]' ? [heading]
    : selector === '*' ? [control] : selector === 'a' ? [] : [];
  let time = 0;
  await assert.rejects(kad.runCard(doc, CASE, {
    cardUrl: CARD, timeoutMs: 4, pollIntervalMs: 1, settleMs: 2,
    now: () => time, sleep: async ms => { time += ms; },
  }), e => e.code === 'page-ready');
  assert.equal(control.clicks, 0);
});

test('PDF candidates must carry the current card UUID in the observed public path and are deduplicated', () => {
  const doc = makeDoc({ href: CARD, title: CASE, bodyText: `${CASE} Электронное дело` });
  const heading = new Element(CASE);
  const longLabel = 'Наименование документа '.repeat(30).trim();
  const matching = link(doc, longLabel, pdfUrl());
  const duplicate = link(doc, 'Дублирующийся элемент', pdfUrl());
  const wrongCard = link(doc, 'wrong card', pdfUrl('99999999-8888-7777-6666-555555555555'));
  const generic = link(doc, 'document.pdf', 'https://kad.arbitr.ru/Document/document.pdf');
  const queried = link(doc, 'query pdf', `${pdfUrl()}?download=1`);
  const credentialed = link(doc, 'userinfo pdf', `https://user@kad.arbitr.ru/Kad/PdfDocument/${CARD_ID}/${DOC_ID}/act.pdf`);
  const fragmented = link(doc, 'fragment pdf', `${pdfUrl()}#preview`);
  const alternatePort = link(doc, 'alternate port', `https://kad.arbitr.ru:444/Kad/PdfDocument/${CARD_ID}/${DOC_ID}/act.pdf`);
  const helper = new Element('helper', { id: 'sro-kad-helper' });
  const panelLink = link(doc, 'panel pdf', pdfUrl(CARD_ID, 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff', 'panel.pdf'));
  panelLink.parentElement = helper;
  doc.querySelectorAll = selector => selector === 'h1, h2, h3, h4, [role=heading]' ? [heading]
    : selector === 'a' ? [matching, duplicate, wrongCard, generic, queried, credentialed, fragmented, alternatePort, panelLink] : [];
  const candidates = kad.collectPdfCandidates(doc, CASE, CARD);
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0].href, pdfUrl());
  assert.equal(candidates[0].label, longLabel);
  assert.equal(candidates[0].cardId, CARD_ID);
});

test('PDF candidate limits fail safely instead of truncating or silently overflowing', () => {
  const doc = makeDoc({ href: CARD, title: CASE, bodyText: `${CASE} Электронное дело` });
  const heading = new Element(CASE);
  const tooLong = link(doc, 'x'.repeat(2001), pdfUrl());
  doc.querySelectorAll = selector => selector === 'h1, h2, h3, h4, [role=heading]' ? [heading]
    : selector === 'a' ? [tooLong] : [];
  assert.throws(() => kad.collectPdfCandidates(doc, CASE, CARD), e => e.code === 'candidate-label-too-long');
  const many = Array.from({ length: 101 }, (_, index) => link(doc, `Act ${index}`, pdfUrl(CARD_ID,
    `${String(index + 1).padStart(8, '0')}-bbbb-cccc-dddd-eeeeeeeeeeee`, `act-${index}.pdf`)));
  doc.querySelectorAll = selector => selector === 'h1, h2, h3, h4, [role=heading]' ? [heading]
    : selector === 'a' ? many : [];
  assert.throws(() => kad.collectPdfCandidates(doc, CASE, CARD), e => e.code === 'candidate-limit');

  const unlabeled = link(doc, '', pdfUrl(CARD_ID, DOC_ID, 'unlabeled.pdf'));
  doc.querySelectorAll = selector => selector === 'h1, h2, h3, h4, [role=heading]' ? [heading]
    : selector === 'a' ? [unlabeled] : [];
  assert.throws(() => kad.collectPdfCandidates(doc, CASE, CARD), e => e.code === 'candidate-label-missing');
});

test('runCard rejects mismatched identity and reports bounded candidate timeout', async () => {
  const mismatch = makeDoc({ href: CARD, title: 'А99-12345/2098', bodyText: 'Электронное дело' });
  mismatch.querySelectorAll = () => [];
  await assert.rejects(kad.runCard(mismatch, CASE, { ...quickOptions(), cardUrl: CARD }), e => e.code === 'timeout');

  const doc = makeDoc({ href: CARD, title: CASE, bodyText: `${CASE} Электронное дело` });
  const heading = new Element(CASE);
  const control = new Element('Электронное дело');
  doc.querySelectorAll = selector => selector === 'h1, h2, h3, h4, [role=heading]' ? [heading]
    : selector === '*' ? [control] : selector === 'a' ? [] : [];
  await assert.rejects(kad.runCard(doc, CASE, { ...quickOptions(), cardUrl: CARD }), e => e.code === 'timeout' && /No visible/.test(e.message));
});

test('runSearch honors AbortSignal with a safe classified error', async () => {
  const fixture = searchDoc({ clickBehavior: () => {} });
  const controller = new AbortController(); controller.abort();
  await assert.rejects(kad.runSearch(fixture.doc, CASE, { ...quickOptions(), signal: controller.signal }), e => e.code === 'aborted');
});
