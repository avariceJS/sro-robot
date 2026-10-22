import assert from "node:assert/strict";
import { test } from "node:test";

test("candidate transfer only copies into the matching editable input and dispatches normal form events", async () => {
  const listeners = {};
  const events = [];
  const field = {
    value: "",
    disabled: false,
    readOnly: false,
    dispatchEvent(event) { events.push(event.type); },
    focus() { events.push("focus"); },
  };
  const button = {
    dataset: {
      reviewSuggestionCopy: "inn",
      suggestionValue: "772881234508",
    },
    textContent: "Перенести в поле",
  };
  globalThis.document = {
    addEventListener(type, callback) { listeners[type] = callback; },
    getElementById(id) { return id === "inn" ? field : null; },
  };

  await import("../app/web/static/review-suggestions.js");
  listeners.click({ target: { closest(selector) {
    assert.equal(selector, "[data-review-suggestion-copy]");
    return button;
  } } });

  assert.equal(field.value, "772881234508");
  assert.deepEqual(events, ["input", "change", "focus"]);
  assert.match(button.textContent, /сохраните изменение/);

  field.disabled = true;
  field.value = "unchanged";
  events.length = 0;
  listeners.click({ target: { closest: () => button } });
  assert.equal(field.value, "unchanged");
  assert.deepEqual(events, []);
});
