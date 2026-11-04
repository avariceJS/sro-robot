import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";

test("field source highlights normalized image boxes and drops them after edits", async () => {
  class SvgElement {
    constructor(tag) { this.tagName = tag; this.attributes = {}; this.children = []; }
    setAttribute(name, value) { this.attributes[name] = value; }
    append(child) { this.children.push(child); }
    querySelector(selector) { return selector === "rect" ? this.children[0] : null; }
    scrollIntoView() {}
    remove() { this.removed = true; }
  }
  const notice = { textContent: "", hidden: true };
  const stage = {
    style: {},
    appended: null,
    append(node) { this.appended = node; },
  };
  const viewport = { clientWidth: 600, clientHeight: 900, scrollIntoView() {} };
  const image = {
    naturalWidth: 1200,
    naturalHeight: 2000,
    style: {},
    dataset: {},
    addEventListener() {},
    closest(selector) {
      if (selector === "[data-source-image-stage]") return stage;
      if (selector === ".image-preview-viewport") return viewport;
      return null;
    },
    scrollIntoView() {},
  };
  const trigger = {
    dataset: {
      fieldSourceTrigger: "inn",
      sourceValue: "772881234508",
      sourceBoxes: JSON.stringify([{ page: 1, x: 0.1, y: 0.2, width: 0.3, height: 0.04 }]),
    },
    listeners: {},
    addEventListener(name, listener) { this.listeners[name] = listener; },
    closest() { return { querySelector() { return notice; } }; },
  };
  const input = {
    id: "inn",
    tagName: "INPUT",
    value: "772881234508",
    listeners: {},
    addEventListener(name, listener) { this.listeners[name] = listener; },
    focus() { this.listeners.focus(); },
  };
  const document = {
    getElementById(id) { return id === "source-image" ? image : id === "inn" ? input : null; },
    querySelectorAll(selector) {
      assert.equal(selector, "[data-field-source-trigger]");
      return [trigger];
    },
    createElementNS(_namespace, tag) { return new SvgElement(tag); },
    addEventListener(name, listener) { this.listeners ||= {}; this.listeners[name] = listener; },
  };
  const source = await readFile(new URL("../app/web/static/field-source.js", import.meta.url), "utf8");
  vm.runInNewContext(source, { document });

  input.listeners.focus();
  assert.equal(stage.appended.tagName, "svg");
  assert.deepEqual(stage.appended.children[0].attributes, {
    x: "100", y: "200", width: "300", height: "40",
  });
  assert.equal(notice.hidden, true);

  document.listeners.focusin({ target: { id: "unmarked", tagName: "INPUT" } });
  assert.equal(stage.appended.removed, true, "moving focus to an unmarked field clears the prior fragment");
  input.listeners.focus();

  input.value = "772881234509";
  input.listeners.input();
  assert.equal(stage.appended.removed, true);
  assert.equal(notice.textContent, "Значение изменено вручную. Источник для него не размечен.");

  trigger.listeners.click();
  assert.equal(stage.appended.removed, true);
});

test("field source ignores malformed and non-first-page boxes", async () => {
  const notices = { textContent: "", hidden: true };
  const stage = { style: {}, append() { assert.fail("invalid boxes must not create an overlay"); } };
  const image = { dataset: {}, naturalWidth: 100, naturalHeight: 200, style: {}, addEventListener() {}, closest(selector) { return selector === "[data-source-image-stage]" ? stage : null; } };
  const trigger = {
    dataset: { fieldSourceTrigger: "name", sourceValue: "Иван", sourceBoxes: JSON.stringify([
      { page: 2, x: 0, y: 0, width: 0.5, height: 0.2 },
      { page: 1, x: 0.8, y: 0, width: 0.4, height: 0.2 },
    ]) },
    addEventListener(name, listener) { this.listeners ||= {}; this.listeners[name] = listener; },
    closest() { return { querySelector() { return notices; } }; },
  };
  const input = { id: "name", value: "Иван", addEventListener(name, listener) { this.listeners ||= {}; this.listeners[name] = listener; } };
  const document = {
    getElementById(id) { return id === "source-image" ? image : input; },
    querySelectorAll() { return [trigger]; },
    addEventListener() {},
  };
  const source = await readFile(new URL("../app/web/static/field-source.js", import.meta.url), "utf8");
  vm.runInNewContext(source, { document });
  input.listeners.focus();
  assert.equal(notices.textContent, "Фрагмент источника на этой странице недоступен.");
});

test("image stage and overlay layout follow tall and resized source images", async () => {
  const resizeCallbacks = [];
  class TestResizeObserver {
    constructor(callback) { this.callback = callback; resizeCallbacks.push(callback); }
    observe() {}
  }
  const stage = { style: {}, append() {} };
  const viewport = { clientWidth: 600, clientHeight: 900 };
  const imageListeners = {};
  const image = {
    naturalWidth: 1200,
    naturalHeight: 2000,
    style: {},
    dataset: {},
    addEventListener(name, listener) { imageListeners[name] = listener; },
    closest(selector) {
      if (selector === "[data-source-image-stage]") return stage;
      if (selector === ".image-preview-viewport") return viewport;
      return null;
    },
  };
  const document = {
    getElementById(id) { return id === "source-image" ? image : null; },
    querySelectorAll() { return []; },
    addEventListener() {},
  };
  const source = await readFile(new URL("../app/web/static/field-source.js", import.meta.url), "utf8");
  vm.runInNewContext(source, { document, ResizeObserver: TestResizeObserver });

  assert.equal(stage.style.width, "540px");
  assert.equal(stage.style.height, "900px");
  assert.equal(image.style.width, stage.style.width);
  assert.equal(image.style.height, stage.style.height);
  assert.equal(image.style.maxWidth, "none");
  assert.equal(image.style.maxHeight, "none");

  viewport.clientWidth = 300;
  resizeCallbacks[0]();
  assert.equal(stage.style.width, "300px");
  assert.equal(stage.style.height, "500px");

  image.naturalWidth = 1800;
  image.naturalHeight = 900;
  imageListeners.load();
  assert.equal(stage.style.width, "300px");
  assert.equal(stage.style.height, "150px");
});

test("field source reports a failed or detached preview and clears any existing overlay", async () => {
  const stage = { style: {}, appended: null, append(node) { this.appended = node; } };
  const notice = { textContent: "", hidden: true };
  const listeners = {};
  const viewport = { clientWidth: 600, clientHeight: 900, scrollIntoView() {} };
  const image = {
    isConnected: true,
    naturalWidth: 600,
    naturalHeight: 900,
    dataset: {},
    style: {},
    addEventListener(name, listener) { listeners[name] = listener; },
    closest(selector) {
      if (selector === "[data-source-image-stage]") return stage;
      if (selector === ".image-preview-viewport") return viewport;
      return null;
    },
  };
  class SvgElement {
    constructor(tag) { this.tagName = tag; this.children = []; }
    setAttribute() {}
    append(child) { this.children.push(child); }
    querySelector() { return this.children[0]; }
    remove() { this.removed = true; }
    scrollIntoView() {}
  }
  const trigger = {
    dataset: { fieldSourceTrigger: "inn", sourceValue: "123", sourceBoxes: JSON.stringify([{ page: 1, x: 0, y: 0, width: 0.2, height: 0.1 }]) },
    addEventListener(name, listener) { this.listeners ||= {}; this.listeners[name] = listener; },
    closest() { return { querySelector() { return notice; } }; },
  };
  const input = { id: "inn", value: "123", addEventListener(name, listener) { this.listeners ||= {}; this.listeners[name] = listener; } };
  const document = {
    getElementById(id) { return id === "source-image" ? image : input; },
    querySelectorAll() { return [trigger]; },
    createElementNS(_namespace, tag) { return new SvgElement(tag); },
    addEventListener() {},
  };
  const source = await readFile(new URL("../app/web/static/field-source.js", import.meta.url), "utf8");
  vm.runInNewContext(source, { document });

  input.listeners.focus();
  const previousOverlay = stage.appended;
  listeners.error();
  assert.equal(previousOverlay.removed, true);
  assert.equal(notice.textContent, "Предпросмотр документа недоступен.");

  image.isConnected = false;
  input.listeners.focus();
  assert.equal(notice.textContent, "Предпросмотр документа недоступен.");
});
