import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";

function harness({ field = "inn", boxes = [{ page: 1, x: .1, y: .2, width: .3, height: .04 }] } = {}) {
  const sourceBounds = { left: 20, top: 30, width: 20, height: 20 };
  class SvgElement {
    constructor(tag) { this.tagName = tag; this.attributes = {}; this.children = []; this.style = {}; this.dataset = {}; this.classes = new Set(); this.classList = { toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name) }; }
    setAttribute(k, v) { this.attributes[k] = v; }
    addEventListener(n, fn) { this.listeners ||= {}; this.listeners[n] = fn; }
    append(child) { this.children.push(child); }
    remove() { this.removed = true; }
    getBoundingClientRect() { return { ...sourceBounds, right: sourceBounds.left + sourceBounds.width, bottom: sourceBounds.top + sourceBounds.height }; }
    querySelectorAll(selector) {
      if (selector === ".source-pointer-hit") return this.children.filter((node) => node.attributes.class === selector.slice(1));
      const match = selector.match(/^(rect|circle)?\[data-source-index(?:="(\d+)")?\]$/);
      return this.children.filter((node) => match && node.dataset.sourceIndex !== undefined
        && (!match[1] || node.tagName === match[1]) && (!match[2] || node.dataset.sourceIndex === match[2]));
    }
  }
  const listeners = {};
  const workspace = { children: [], append(node) { this.children.push(node); }, getBoundingClientRect() { return { left: 0, top: 0, width: 1000, height: 1000 }; }, addEventListener() {} };
  const stage = { style: {}, children: [], closest() { return workspace; }, addEventListener() {}, append(node) { this.children.push(node); }, };
  const viewport = { clientWidth: 600, clientHeight: 900, addEventListener() {}, getBoundingClientRect() { return { left: 0, top: 0, right: 600, bottom: 900 }; } };
  const imgListeners = {};
  const image = { naturalWidth: 1200, naturalHeight: 2000, clientWidth: 540, clientHeight: 900, style: {}, dataset: {}, isConnected: true,
    addEventListener(n, fn) { imgListeners[n] = fn; },
    closest(sel) { return sel === "[data-source-image-stage]" ? stage : sel === ".image-preview-viewport" ? viewport : null; },
    getBoundingClientRect() { return { left: 20, top: 30, width: 540, height: 900 }; } };
  const notice = { textContent: "", hidden: true };
  const label = { addEventListener(n, fn) { this.listeners ||= {}; this.listeners[n] = fn; }, getBoundingClientRect() { return { left: 700, top: 90, width: 70, height: 22 }; } };
  const marker = { textContent: "1", hidden: false, dataset: { fieldSourceMarker: field, sourceValue: "123", sourceBoxes: JSON.stringify(boxes) }, style: {},
    addEventListener(n, fn) { this.listeners ||= {}; this.listeners[n] = fn; },
    closest() { return { querySelector(sel) { return sel === "label" ? label : notice; } }; }, getBoundingClientRect() { return { left: 700, top: 90, width: 22, height: 22 }; } };
  const input = { id: field, value: "123", addEventListener(n, fn) { this.listeners ||= {}; this.listeners[n] = fn; }, getBoundingClientRect() { return { left: 650, top: 90, width: 300, height: 30 }; } };
  const document = { getElementById(id) { return id === "source-image" ? image : id === field ? input : null; },
    querySelectorAll(sel) { return sel === "[data-field-source-marker]" ? [marker] : []; },
    createElementNS(_ns, tag) { return new SvgElement(tag); }, };
  return { image, imgListeners, stage, workspace, marker, label, input, notice, sourceBounds, windowEvents: {}, source: readFile(new URL("../app/web/static/field-source.js", import.meta.url), "utf8"), document, listeners };
}

test("mapped source boxes and arrows appear automatically without source image labels", async () => {
  const h = harness();
  vm.runInNewContext(await h.source, { document: h.document, window: { addEventListener(n, fn) { h.windowEvents[n] = fn; } } });
  const overlay = h.stage.children.find((node) => node.tagName === "svg");
  assert.ok(overlay, "image overlay is rendered on page load");
  const bounds = overlay.children.filter((node) => node.tagName === "rect")[0].attributes;
  assert.ok(Math.abs((100 - Number(bounds.x)) * 540 / 1000 - 1) < 1e-9, "one CSS pixel left padding");
  assert.ok(Math.abs((200 - Number(bounds.y)) * 900 / 1000 - 1) < 1e-9, "one CSS pixel top padding");
  assert.ok(Math.abs((Number(bounds.width) - 300) * 540 / 1000 - 2) < 1e-9, "one CSS pixel on both horizontal sides");
  assert.equal(overlay.children.some((node) => node.tagName === "circle" || node.tagName === "text"), false, "number marker is not drawn on the source image");
  h.input.listeners.focus();
  const rect = overlay.children.find((node) => node.tagName === "rect");
  assert.equal(rect.classes.has("is-active"), true);
  h.label.listeners.mouseenter();
  assert.equal(rect.classes.has("is-active"), true, "field label hover emphasizes its source rectangle");

  const pointers = h.workspace.children.find((node) => node.tagName === "svg" && node.children.some((child) => child.tagName === "line"));
  const line = pointers.children.find((child) => child.tagName === "line");
  assert.equal(line.attributes.x1, "40", "connector starts at the mapped rectangle edge");
  assert.equal(line.attributes.x2, "696", "connector targets just before the source field label");
  assert.equal(pointers.attributes["aria-hidden"], undefined, "accessible pointer controls are exposed");
  const hit = pointers.children.find((child) => child.attributes.class === "source-pointer-hit");
  assert.equal(hit.attributes.role, "button"); assert.equal(hit.attributes["aria-label"], "Закрепить источник: inn");
  hit.listeners.click();
  assert.equal(hit.attributes["aria-pressed"], "true");
  hit.listeners.mouseleave(); h.windowEvents.scroll();
  let current = h.workspace.children.filter((node) => node.tagName === "svg" && !node.removed).at(-1);
  let overlayAfterRerender = h.stage.children.filter((node) => node.tagName === "svg" && !node.removed).at(-1);
  assert.equal(overlayAfterRerender.children.find((child) => child.tagName === "rect").classes.has("is-active"), true, "pin survives pointer leave and rerender");
  current.children.find((child) => child.attributes.class === "source-pointer-hit").listeners.click();
  current = h.workspace.children.filter((node) => node.tagName === "svg" && !node.removed).at(-1);
  overlayAfterRerender = h.stage.children.filter((node) => node.tagName === "svg" && !node.removed).at(-1);
  assert.equal(overlayAfterRerender.children.find((child) => child.tagName === "rect").classes.has("is-active"), false, "second click unpins without retaining hover");
  const keyboardHit = current.children.find((child) => child.attributes.class === "source-pointer-hit");
  let prevented = false;
  keyboardHit.listeners.keydown({ key: "Enter", preventDefault() { prevented = true; } });
  assert.equal(prevented, true); assert.equal(keyboardHit.attributes["aria-pressed"], "true", "Enter pins an accessible source arrow");
  keyboardHit.listeners.keydown({ key: " ", preventDefault() {} });
  assert.equal(keyboardHit.attributes["aria-pressed"], "false", "Space unpins it");
  h.input.value = "124"; h.input.listeners.input();
  assert.equal(h.workspace.children.at(-1).children.some((node) => node.attributes.class === "source-pointer-hit"), false);
  assert.equal(h.notice.textContent, "Значение изменено вручную. Источник для него не размечен.");
  h.input.value = "123"; h.input.listeners.input();
  assert.equal(h.workspace.children.at(-1).children.some((node) => node.attributes.class === "source-pointer-hit"), true, "restoring the recognized value restores its pointer");
  assert.equal(h.notice.hidden, true, "the stale-source notice is cleared after restoration");
});

test("invalid and non-first-page geometry is withheld", async () => {
  const h = harness({ boxes: [{ page: 2, x: 0, y: 0, width: .5, height: .2 }, { page: 1, x: .8, y: 0, width: .4, height: .2 }] });
  vm.runInNewContext(await h.source, { document: h.document, window: { addEventListener() {} } });
  assert.equal(h.workspace.children.at(-1).children.some((node) => node.attributes.class === "source-pointer-hit"), false);
});

test("tall images retain fit sizing and failed previews remove pointers", async () => {
  const h = harness();
  const resize = [];
  class RO { constructor(fn) { resize.push(fn); } observe() {} }
  vm.runInNewContext(await h.source, { document: h.document, window: { addEventListener() {} }, ResizeObserver: RO });
  assert.equal(h.stage.style.width, "540px"); assert.equal(h.stage.style.height, "900px");
  const existing = h.stage.children.find((node) => node.tagName === "svg");
  h.imgListeners.error(); assert.equal(existing.removed, true);
  assert.equal(h.stage.children.length, 1, "failed preview removes mapped overlay");
  assert.equal(h.notice.textContent, "Предпросмотр документа недоступен.");
  h.imgListeners.load();
  assert.equal(h.notice.hidden, true, "successful image recovery clears its preview error");
});

test("scrolling recalculates source pointers and hides a connector to a clipped source", async () => {
  const h = harness();
  const events = {};
  vm.runInNewContext(await h.source, { document: h.document, window: { addEventListener(name, callback) { events[name] = callback; } } });
  assert.equal(typeof events.scroll, "function");
  h.sourceBounds.left = -50;
  events.scroll();
  let pointers = h.workspace.children.filter((node) => node.tagName === "svg" && !node.removed).at(-1);
  assert.equal(pointers.children.some((node) => node.tagName === "line"), false);
  h.sourceBounds.left = 100;
  events.scroll();
  pointers = h.workspace.children.filter((node) => node.tagName === "svg" && !node.removed).at(-1);
  const line = pointers.children.find((node) => node.tagName === "line");
  assert.equal(line.attributes.x1, "120");
  h.sourceBounds.left = 590;
  events.scroll();
  pointers = h.workspace.children.filter((node) => node.tagName === "svg" && !node.removed).at(-1);
  assert.equal(pointers.children.find((node) => node.tagName === "line").attributes.x1, "600", "partially clipped source starts at visible edge");
});

test("same-line date words form a single padded rectangle", async () => {
  const h = harness({ field: "doc_date", boxes: [
    { page: 1, x: .1, y: .2, width: .02, height: .04 },
    { page: 1, x: .14, y: .198, width: .1, height: .045 },
    { page: 1, x: .26, y: .201, width: .06, height: .04 },
  ] });
  vm.runInNewContext(await h.source, { document: h.document, window: { addEventListener() {} } });
  const rects = h.stage.children.find((node) => node.tagName === "svg").children.filter((node) => node.tagName === "rect");
  assert.equal(rects.length, 1, "day, month and year share one outline");
  const bounds = rects[0].attributes;
  assert.ok(Number(bounds.x) < 100 && Number(bounds.x) + Number(bounds.width) > 320);
  assert.ok(Number(bounds.y) < 198 && Number(bounds.y) + Number(bounds.height) > 243);
});
