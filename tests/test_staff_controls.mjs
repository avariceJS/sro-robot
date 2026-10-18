import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";

test("failed source image preview gives staff a safe original-download fallback", async () => {
  class Element {
    constructor() {
      this.attributes = {};
    }

    closest() {
      return null;
    }

    setAttribute(name, value) {
      this.attributes[name] = value;
    }
  }

  class HTMLImageElement extends Element {
    constructor() {
      super();
      this.id = "source-image";
      this.replacement = null;
    }

    closest(selector) {
      return selector === ".image-preview-viewport" ? viewport : null;
    }

    replaceWith(element) {
      this.replacement = element;
    }
  }

  const listeners = {};
  const viewport = new Element();
  const image = new HTMLImageElement();
  const document = {
    addEventListener(name, listener, capture) {
      listeners[name] = { listener, capture };
    },
    getElementById(id) {
      return id === "source-image" ? image : null;
    },
    querySelectorAll() {
      return [];
    },
    createElement(tag) {
      assert.equal(tag, "p");
      return new Element();
    },
  };
  const source = await readFile(new URL("../app/web/static/staff-controls.js", import.meta.url), "utf8");
  vm.runInNewContext(source, {
    document, Element, HTMLImageElement, Event,
    window: { addEventListener() {} },
  });

  assert.equal(listeners.error.capture, true);
  listeners.error.listener({ target: image });
  assert.equal(image.replacement.className, "field-warning");
  assert.equal(image.replacement.attributes.role, "status");
  assert.match(image.replacement.textContent, /Скачайте оригинал выше/);
});

test("assignment edit warns before leaving changed values, but allows restore and submit", async () => {
  const documentListeners = {};
  const windowListeners = {};
  const formListeners = {};
  const field = {
    tagName: "INPUT", type: "text", value: "Исходное значение",
    defaultValue: "Исходное значение", disabled: false,
  };
  const inactiveEntityField = {
    tagName: "INPUT", type: "text", value: "",
    defaultValue: "", disabled: true,
  };
  const form = {
    elements: [field, inactiveEntityField],
    addEventListener(name, listener) {
      formListeners[name] = listener;
    },
  };
  const document = {
    addEventListener(name, listener) {
      documentListeners[name] = listener;
    },
    querySelectorAll(selector) {
      assert.equal(selector, "form[data-unsaved-guard]");
      return [form];
    },
    getElementById() {
      return null;
    },
  };
  const window = {
    addEventListener(name, listener) {
      windowListeners[name] = listener;
    },
  };
  const source = await readFile(new URL("../app/web/static/staff-controls.js", import.meta.url), "utf8");
  vm.runInNewContext(source, { document, window, Element: class Element {}, Event });

  const changed = { returnValue: undefined, prevented: false, preventDefault() { this.prevented = true; } };
  field.value = "Изменённое значение";
  windowListeners.beforeunload(changed);
  assert.equal(changed.prevented, true);
  assert.equal(changed.returnValue, "");

  field.value = field.defaultValue;
  inactiveEntityField.value = "Введённое значение в другой тип карточки";
  const changedWhileInactive = {
    returnValue: undefined,
    prevented: false,
    preventDefault() { this.prevented = true; },
  };
  windowListeners.beforeunload(changedWhileInactive);
  assert.equal(changedWhileInactive.prevented, true);

  inactiveEntityField.value = inactiveEntityField.defaultValue;
  field.value = field.defaultValue;
  const restored = { returnValue: undefined, prevented: false, preventDefault() { this.prevented = true; } };
  windowListeners.beforeunload(restored);
  assert.equal(restored.prevented, false);

  field.value = "Изменённое значение";
  formListeners.submit();
  const submitting = { returnValue: undefined, prevented: false, preventDefault() { this.prevented = true; } };
  windowListeners.beforeunload(submitting);
  assert.equal(submitting.prevented, false);
  assert.deepEqual(Object.keys(documentListeners), ["error", "click"]);
});
