import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";

test("entity selection enables only the matching debtor-card fields", async () => {
  const listeners = {};
  const select = {
    value: "person",
    addEventListener(name, listener) {
      listeners[name] = listener;
    },
  };
  const groups = ["person", "legal_entity", "sole_proprietor"].map((kind) => {
    const field = { disabled: false };
    return {
      dataset: { entityKindFields: kind },
      hidden: false,
      field,
      querySelectorAll(selector) {
        assert.equal(selector, "input, textarea, select");
        return [field];
      },
    };
  });
  const form = {
    querySelector(selector) {
      assert.equal(selector, "[data-entity-kind-select]");
      return select;
    },
    querySelectorAll(selector) {
      assert.equal(selector, "[data-entity-kind-fields]");
      return groups;
    },
  };
  const document = {
    querySelector(selector) {
      assert.equal(selector, "[data-entity-kind-form]");
      return form;
    },
  };
  const source = await readFile(
    new URL("../app/web/static/debtor-entity-kind.js", import.meta.url),
    "utf8",
  );

  vm.runInNewContext(source, { document });
  const assertSelected = (kind) => {
    for (const group of groups) {
      const active = group.dataset.entityKindFields === kind;
      assert.equal(group.hidden, !active);
      assert.equal(group.field.disabled, !active);
    }
  };

  assertSelected("person");
  select.value = "legal_entity";
  listeners.change();
  assertSelected("legal_entity");
  select.value = "sole_proprietor";
  listeners.change();
  assertSelected("sole_proprietor");
});
