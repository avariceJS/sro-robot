import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const ui = require('../sro_robot/research/static/review.js');

test('appointment approval needs the server evidence gate; empty scope can reject', () => {
  const doc = {revision: 2, snapshot_sha256: 'b'.repeat(64), review_projection: {
    review_kind: 'appointment', applicable_fields: [], approval_allowed: false
  }};
  assert.throws(() => ui.makeVerdictPayload(doc, 'approved', 'synthetic-action'), /appointment_evidence_incomplete/);
  assert.throws(() => ui.makeVerdictPayload(doc, 'rejected', 'synthetic-action'), /rejection_reason_required/);
  const rejection = ui.makeVerdictPayload(doc, 'rejected', 'synthetic-action', 'Нет распознанных полей');
  assert.deepEqual(rejection.reviewed_fields, []);
  assert.deepEqual(rejection.corrected_fields, {});
  assert.deepEqual(rejection.incorrect_fields, []);
  assert.equal(rejection.comment, 'Нет распознанных полей');
});

test('appointment approval uses the exact partial displayed scope without inventing optional fields', () => {
  const scope = ['case_number', 'hearing_date', 'hearing_time'];
  const doc = {revision: 3, snapshot_sha256: 'c'.repeat(64), review_projection: {
    review_kind: 'appointment', applicable_fields: scope, approval_allowed: true
  }};
  const payload = ui.makeVerdictPayload(doc, 'approved', 'synthetic-action');
  assert.deepEqual(payload.reviewed_fields, scope);
  assert.equal(ui.APPOINTMENT_LABELS.hearing_time, 'Время судебного заседания');
  assert.equal(ui.APPOINTMENT_LABELS.doc_date, 'Дата судебного акта');
  assert.equal(ui.FIELDS.length, 20);
});
