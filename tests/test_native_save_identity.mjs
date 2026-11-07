import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

// Execute the maintained JXA identity functions with a synthetic AX model.
// No browser, OCR, source documents or UI actions are used by this test.
const source = readFileSync(new URL('../scripts/dev/quality/download_current_kad.py', import.meta.url), 'utf8');
const identityCode = source.slice(source.indexOf('function windowTitleMatches('), source.indexOf('function waitFor(')).replaceAll('{{', '{').replaceAll('}}', '}');
const expected = {url: 'synthetic-pdf', id: 'synthetic-tab', window: 'synthetic-window', title: 'Synthetic act', bounds: [10, 30, 1010, 830]};
function ax(role, name, x, y, width, height) {
  return { role: () => role, name: () => name, position: () => [x,y], size: () => [width,height], children: [] };
}
function model() {
  const document = ax('AXWindow', expected.title + ' - Google Chrome', 10, 30, 1000, 800);
  const sheet = ax('AXSheet', 'Synthetic Save', 120, 120, 600, 400);
  document.children.push(sheet);
  const context = {
    expectedURL: expected.url, openedByUs: true, lastSaveGroups: [],
    role: e => e.role(), items: e => e.children,
    coordinatePair: value => value, normalizedRect: value => value,
    currentIdentity: () => ({...expected}),
    focused: sheet, windows: [document], groups: [{rootWindow: document, parent: sheet}],
  };
  context.process = {windows: () => context.windows, attributes: {byName: () => ({value: () => context.focused})}};
  context.findSaveGroups = () => context.groups;
  vm.createContext(context); vm.runInContext(identityCode, context);
  return {context, document, sheet, matches: (modal=true) => context.sameIdentity(expected, modal)};
}

test('strict preflight requires the source document key, then accepts its owned Save sheet', () => {
  const m = model();
  assert.equal(m.matches(false), false);
  assert.equal(m.matches(true), true);
  m.context.focused = m.document; m.context.groups = [];
  assert.equal(m.matches(false), true);
});

test('inline Go To Folder group retains the proven Save-sheet key and ancestry', () => {
  const m = model();
  m.sheet.children.push(ax('AXGroup', 'Synthetic inline folder input', 180, 160, 400, 120));
  assert.equal(m.matches(), true);
});

test('document focus cannot bypass ownership of a present unrelated Save sheet', () => {
  const m = model();
  m.context.focused = m.document;
  m.context.groups[0].rootWindow = ax('AXWindow', 'Unrelated window', 10, 30, 1000, 800);
  assert.equal(m.matches(), false);
});

test('foreign or duplicate sheets and distinct unproven folder dialogs fail closed', () => {
  const m = model();
  m.context.focused = ax('AXSheet', 'Foreign Save', 120, 120, 600, 400);
  assert.equal(m.matches(), false);
  m.context.focused = ax('AXDialog', 'Synthetic Go To Folder', 120, 120, 600, 400);
  assert.equal(m.matches(), false);
  m.context.focused = m.sheet; m.context.groups.push({...m.context.groups[0]});
  assert.equal(m.matches(), false);
});

test('modal proof never relaxes URL, tab, window, source geometry or opened-by-us binding', () => {
  for (const mutate of [
    m => {m.context.currentIdentity = () => ({...expected, url: 'different-pdf'});},
    m => {m.context.currentIdentity = () => ({...expected, id: 'different-tab'});},
    m => {m.context.currentIdentity = () => ({...expected, window: 'different-window'});},
    m => {m.document.position = () => [300,300];},
    m => {m.context.openedByUs = false;},
  ]) {const m = model(); mutate(m); assert.equal(m.matches(), false);}
});
