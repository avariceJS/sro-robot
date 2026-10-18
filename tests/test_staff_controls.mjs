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
    createElement(tag) {
      assert.equal(tag, "p");
      return new Element();
    },
  };
  const source = await readFile(new URL("../app/web/static/staff-controls.js", import.meta.url), "utf8");
  vm.runInNewContext(source, { document, Element, HTMLImageElement });

  assert.equal(listeners.error.capture, true);
  listeners.error.listener({ target: image });
  assert.equal(image.replacement.className, "field-warning");
  assert.equal(image.replacement.attributes.role, "status");
  assert.match(image.replacement.textContent, /Скачайте оригинал выше/);
});
