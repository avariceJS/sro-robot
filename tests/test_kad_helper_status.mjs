import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../app/web/static/kad-helper-status.js', import.meta.url), 'utf8');

class FakeElement {
  constructor(selector = '') { this.selector = selector; }
  closest(selector) { return selector === '[data-kad-store-link]' && this.selector === 'store-link' ? this : null; }
}

function harness({storesReady = true} = {}) {
  const events = new Map();
  const nodes = {
    '#kad-helper-connection-status': {dataset: {state: 'checking', storesReady: String(storesReady)}, textContent: ''},
    '#kad-helper-connection-indicator': {dataset: {state: 'checking'}},
    '#kad-helper-install': storesReady ? {hidden: false} : null,
    '#kad-helper-install-unconfigured': storesReady ? null : {hidden: true},
    '#kad-helper-connected': {hidden: true},
  };
  const session = new Map();
  let timer;
  let observer;
  let reloads = 0;
  const document = {
    visibilityState: 'visible',
    addEventListener(name, handler) { events.set(name, handler); },
    querySelector(selector) { return nodes[selector] ?? null; },
  };
  class MutationObserver {
    constructor(callback) { observer = callback; }
    observe() {}
  }
  const context = {
    document,
    Element: FakeElement,
    MutationObserver,
    sessionStorage: {
      getItem(key) { return session.get(key) ?? null; },
      setItem(key, value) { session.set(key, String(value)); },
      removeItem(key) { session.delete(key); },
    },
    location: {reload() { reloads += 1; }},
    window: {setTimeout(callback) { timer = callback; }},
  };
  vm.runInNewContext(source, context, {filename: 'kad-helper-status.js'});
  return {
    document, nodes, events, session,
    finishMissingCheck() { timer(); },
    extensionState(state, text) {
      nodes['#kad-helper-connection-status'].dataset.state = state;
      nodes['#kad-helper-connection-status'].textContent = text;
      observer();
    },
    clickStoreLink() { events.get('click')({target: new FakeElement('store-link')}); },
    setVisibility(state) {
      document.visibilityState = state;
      events.get('visibilitychange')();
    },
    get reloads() { return reloads; },
  };
}

test('missing helper reveals the configured install CTA after the checking state', () => {
  const h = harness({storesReady: true});
  assert.equal(h.nodes['#kad-helper-install']?.hidden ?? true, true);
  assert.equal(h.nodes['#kad-helper-connected'].hidden, true);

  h.finishMissingCheck();

  assert.equal(h.nodes['#kad-helper-connection-status'].dataset.state, 'unavailable');
  assert.match(h.nodes['#kad-helper-connection-status'].textContent, /Установите его из магазина/);
  assert.equal(h.nodes['#kad-helper-install'].hidden, false);
  assert.equal(h.nodes['#kad-helper-connected'].hidden, true);
  assert.equal(h.nodes['#kad-helper-connection-indicator'].dataset.state, 'unavailable');
});

test('ready state switches to search; permission state keeps the bridge message without install CTA', () => {
  const h = harness();
  h.extensionState('ready', 'Помощник подключён и готов к поиску.');
  assert.equal(h.nodes['#kad-helper-install'].hidden, true);
  assert.equal(h.nodes['#kad-helper-connected'].hidden, false);
  assert.equal(h.nodes['#kad-helper-connection-indicator'].dataset.state, 'ready');

  h.extensionState('permission', 'Включите передачу PDF в настройках браузера.');
  assert.equal(h.nodes['#kad-helper-install'].hidden, true);
  assert.equal(h.nodes['#kad-helper-connected'].hidden, true);
  assert.equal(h.nodes['#kad-helper-connection-status'].textContent, 'Включите передачу PDF в настройках браузера.');
  assert.equal(h.nodes['#kad-helper-connection-indicator'].dataset.state, 'permission');
});

test('return from the store consumes the reload intent once', () => {
  const h = harness();
  h.clickStoreLink();
  assert.equal(h.session.get('sro-kad-helper-install-return-v1'), '1');
  h.setVisibility('hidden');
  h.setVisibility('visible');
  assert.equal(h.reloads, 1);
  assert.equal(h.session.has('sro-kad-helper-install-return-v1'), false);
  h.setVisibility('visible');
  assert.equal(h.reloads, 1);
});

test('unconfigured store fallback appears only after the helper check is unavailable', () => {
  const h = harness({storesReady: false});
  h.finishMissingCheck();
  assert.equal(h.nodes['#kad-helper-install-unconfigured'].hidden, false);
  assert.match(h.nodes['#kad-helper-connection-status'].textContent, /ссылка установки пока не настроена/i);
  assert.equal(h.nodes['#kad-helper-install']?.hidden ?? true, true);
});
