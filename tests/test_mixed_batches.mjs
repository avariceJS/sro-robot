import test from "node:test";
import assert from "node:assert/strict";
import mixedBatches from "../app/web/static/mixed-batches.js";

test("mixed batch labels expose waiting, failure and preview states", () => {
  assert.equal(mixedBatches.statusText({ status: "recognizing" }), "Распознаётся");
  assert.equal(mixedBatches.statusText({ status: "failed" }), "Ошибка обработки");
  assert.equal(mixedBatches.statusText({ status: "awaiting_preview" }), "Фотографии заполнены; карточка готова к проверке");
  assert.equal(mixedBatches.statusText({ status: null }), "Файл пока не принят сервером");
});

test("unuploaded files never claim OCR is running or pending", () => {
  assert.equal(mixedBatches.recognitionStatusText({task_id: null, status: null}), "Файл ещё не загружен");
  assert.equal(mixedBatches.recognitionStatusText({task_id: 12, status: "new"}), "Ожидает распознавания");
  assert.equal(mixedBatches.recognitionStatusText({task_id: 12, status: "recognizing"}), "Распознаётся");
});

test("request keys are fresh 192-bit lowercase hex values", () => {
  let next = 0;
  const cryptoApi = { getRandomValues: (target) => { target.fill(next++); return target; } };
  const first = mixedBatches.randomRequestKey(cryptoApi);
  const second = mixedBatches.randomRequestKey(cryptoApi);
  assert.match(first, /^[0-9a-f]{48}$/);
  assert.notEqual(first, second);
});

test("non-JSON and unsuccessful responses are reported as failures", async () => {
  await assert.rejects(
    mixedBatches.readJSON({ ok: false, status: 409, json: async () => ({ detail: "Изменилось состояние" }) }),
    /Изменилось состояние/,
  );
  await assert.rejects(
    mixedBatches.readJSON({ ok: true, status: 200, json: async () => { throw new Error("invalid json"); } }),
    /HTTP 200/,
  );
});

test("upload preparation summary reports per-file server outcomes", () => {
  assert.equal(
    mixedBatches.preparationSummary([
      { status: "admitted" }, { status: "pending_ocr" }, { status: "withheld" },
    ]),
    "создание карточки поставлено в очередь: 1 · ждут OCR до создания карточки: 1 · удержано для проверки: 1",
  );
});

test("uncertain Add and preparation statuses stay explicit and non-retryable", () => {
  assert.match(mixedBatches.resultMessage("technical_card_add_unknown"), /нужна сверка в СРО перед повтором/);
  assert.match(mixedBatches.resultMessage("technical_card_preparing"), /Подготовка технической карточки выполняется/);
});

test("queued preparation is not offered as a user retry", () => {
  assert.equal(mixedBatches.shouldShowPrepare({ can_prepare: true, prepare_status: "admitted" }), false);
  assert.equal(mixedBatches.shouldShowPrepare({ can_prepare: true, prepare_status: "not_requested" }), true);
  assert.equal(mixedBatches.shouldShowPrepare({ can_prepare: true, technical_card_state: "add_unknown" }), false);
  assert.equal(mixedBatches.prepareButtonLabel({ prepare_status: "queue_uncertain" }), "Повторить передачу подготовки");
});

test("applicant rows show assignment needs without SRO preparation language", () => {
  const text = mixedBatches.assignmentStatusText({
    reason: "explicit_applicant", assignment_url: "/assignments/3",
    assignment_needs_debtor: true, assignment_needs_practitioner: true,
  });
  assert.equal(text, "Черновик назначения: нужно выбрать карточку должника; нужны сведения об управляющем");
  assert.equal(mixedBatches.assignmentStatusText({ reason: "explicit_person_debtor", assignment_url: "/assignments/3" }), "");
});

test("recognition-only preview status does not imply SRO photos or save", () => {
  assert.equal(mixedBatches.recognitionStatusText({ status: "awaiting_preview" }), "Распознано; поля требуют проверки");
  assert.equal(mixedBatches.recognitionStatusText({ status: "recognizing" }), "Распознаётся");
});

test("save confirmation includes the exact selected count", () => {
  assert.equal(mixedBatches.saveConfirmation(1), "Сохранить в СРО выбранные карточки: 1. Вы подтверждаете, что проверили предложенную роль, поля и снимки?");
  assert.equal(mixedBatches.saveConfirmation(21), "Сохранить в СРО выбранные карточки: 21. Вы подтверждаете, что проверили предложенную роль, поля и снимки?");
});

test("external save outcomes stay distinct from local reconciliation", () => {
  assert.match(mixedBatches.resultMessage("save_started"), /ожидается сверка результата/);
  assert.match(mixedBatches.resultMessage("saved"), /подтверждено/);
  assert.match(mixedBatches.resultMessage("save_unknown"), /перед любым повтором/);
  assert.match(mixedBatches.resultMessage("state_uncertain"), /перед любым повтором/);
  assert.match(mixedBatches.resultMessage("local_finalization_succeeded"), /внутреннее оформление завершено/);
  for (const status of ["state_uncertain", "queue_uncertain", "stale_or_unavailable", "save_started", "saved", "save_unknown"]) {
    assert.equal(mixedBatches.blocksSaveRetry(status), true);
  }
  assert.equal(mixedBatches.blocksSaveRetry("save_requested"), false);
});


test("list refresh coalesces overlapping reads and rejects stale success and error", async () => {
  for (const fail of [false, true]) {
    const pending = [], applied = [], errors = [];
    const refresh = mixedBatches.createListRefresher(
      () => new Promise((resolve, reject) => pending.push({resolve, reject})),
      (state) => applied.push(state), (error) => errors.push(error),
    );
    const first = refresh();
    await Promise.resolve();
    const second = refresh();
    refresh.invalidate();
    refresh();
    assert.equal(first, second);
    assert.equal(pending.length, 1);
    if (fail) pending[0].reject(new Error("stale")); else pending[0].resolve("stale");
    await new Promise(setImmediate);
    assert.equal(pending.length, 2);
    assert.deepEqual(applied, []);
    assert.deepEqual(errors, []);
    pending[1].resolve("fresh");
    await first;
    assert.deepEqual(applied, ["fresh"]);
  }
});

test("mutation invalidates in-flight state and pauses polling until completion", async () => {
  let allowed = true, resolve;
  const applied = [];
  const refresh = mixedBatches.createListRefresher(
    () => new Promise((done) => { resolve = done; }),
    (state) => applied.push(state), assert.fail, () => allowed,
  );
  const first = refresh();
  await Promise.resolve();
  refresh.invalidate();
  allowed = false;
  resolve("before mutation");
  await first;
  assert.deepEqual(applied, []);
  allowed = true;
  const second = refresh();
  await Promise.resolve();
  resolve("after mutation");
  await second;
  assert.deepEqual(applied, ["after mutation"]);
});


test("slow responses still render when polling ticks during every GET", async () => {
  const pending = [], applied = [];
  const refresh = mixedBatches.createListRefresher(
    () => new Promise((resolve) => pending.push(resolve)),
    (state) => applied.push(state), assert.fail,
  );
  const done = refresh();
  await Promise.resolve();
  refresh(); // A timer tick while the first GET takes longer than the interval.
  pending[0]("first valid state");
  await new Promise(setImmediate);
  assert.deepEqual(applied, ["first valid state"]);
  pending[1]("second valid state");
  await done;
  assert.deepEqual(applied, ["first valid state", "second valid state"]);
});
