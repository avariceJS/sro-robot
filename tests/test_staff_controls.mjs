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
      if (selector === "form[data-unsaved-guard]") return [form];
      if (selector === "form[data-unsaved-submit-target]") return [];
      assert.fail(`Unexpected selector ${selector}`);
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

test("explicit review recovery submit protects proposals without a false leave warning", async () => {
  const windowListeners = {};
  const guardedListeners = {};
  const recoveryListeners = {};
  const guardedForm = {
    id: "review-form",
    dataset: { unsavedInitiallyDirty: "true" },
    elements: [],
    addEventListener(name, listener) { guardedListeners[name] = listener; },
  };
  const recoveryForm = {
    dataset: { unsavedSubmitTarget: "review-form" },
    addEventListener(name, listener) { recoveryListeners[name] = listener; },
  };
  const document = {
    addEventListener() {},
    querySelectorAll(selector) {
      if (selector === "form[data-unsaved-guard]") return [guardedForm];
      if (selector === "form[data-unsaved-submit-target]") return [recoveryForm];
      assert.fail(`Unexpected selector ${selector}`);
    },
    getElementById() { return null; },
  };
  const source = await readFile(new URL("../app/web/static/staff-controls.js", import.meta.url), "utf8");
  vm.runInNewContext(source, {
    document,
    window: { addEventListener(name, listener) { windowListeners[name] = listener; } },
    Element: class Element {},
    Event,
  });

  const leavingBeforeRecovery = { prevented: false, preventDefault() { this.prevented = true; } };
  windowListeners.beforeunload(leavingBeforeRecovery);
  assert.equal(leavingBeforeRecovery.prevented, true, "unsaved proposals warn before leaving recovery");

  recoveryListeners.submit();
  const intentionalRecoveryPost = { prevented: false, preventDefault() { this.prevented = true; } };
  windowListeners.beforeunload(intentionalRecoveryPost);
  assert.equal(intentionalRecoveryPost.prevented, false, "the explicit recovery POST is not interrupted");

  guardedListeners.submit();
  const ordinarySave = { prevented: false, preventDefault() { this.prevented = true; } };
  windowListeners.beforeunload(ordinarySave);
  assert.equal(ordinarySave.prevented, false, "the ordinary guarded form still allows its submitted save");
});

test("server-returned unsaved values still warn before leaving the form", async () => {
  const windowListeners = {};
  const formListeners = {};
  const form = {
    dataset: { unsavedInitiallyDirty: "true" },
    elements: [{ tagName: "INPUT", type: "text", value: "Invalid but retained", defaultValue: "Invalid but retained" }],
    addEventListener(name, listener) {
      formListeners[name] = listener;
    },
  };
  const document = {
    addEventListener() {},
    querySelectorAll() {
      return [form];
    },
    getElementById() {
      return null;
    },
  };
  const source = await readFile(new URL("../app/web/static/staff-controls.js", import.meta.url), "utf8");
  vm.runInNewContext(source, {
    document,
    window: {
      addEventListener(name, listener) {
        windowListeners[name] = listener;
      },
    },
    Element: class Element {},
    Event,
  });

  const leaving = { prevented: false, preventDefault() { this.prevented = true; } };
  windowListeners.beforeunload(leaving);
  assert.equal(leaving.prevented, true);

  form.dataset.unsavedInitiallyDirty = "false";
  form.elements[0].value = "";
  form.elements[0].defaultValue = "";
  const discarded = { prevented: false, preventDefault() { this.prevented = true; } };
  windowListeners.beforeunload(discarded);
  assert.equal(discarded.prevented, false);

  formListeners.submit();
  const submitting = { prevented: false, preventDefault() { this.prevented = true; } };
  windowListeners.beforeunload(submitting);
  assert.equal(submitting.prevented, false);
});
