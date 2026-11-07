import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFile} from 'node:fs/promises';
const require = createRequire(import.meta.url);
const ui = require('../sro_robot/research/static/review.js');
const original = Object.fromEntries(ui.FIELDS.map(([key]) => [key, {value:null}]));
const values = Object.fromEntries(ui.FIELDS.map(([key]) => [key, '']));
const doc = {id:'synthetic-document',revision:3,snapshot_sha256:'a'.repeat(64),original_fields:original};

test('all17 source fields remain separate; empty approval does not invent absence', () => {
  assert.equal(ui.FIELDS.length,17); assert.equal(new Set(ui.FIELDS.map(([key])=>key)).size,17);
  const payload=ui.makePayload(doc,values,[],'','approved','stable-action');
  assert.deepEqual(payload.corrected_fields,{});assert.deepEqual(payload.incorrect_fields,[]);
  assert.equal(payload.expected_revision,3);assert.equal(payload.expected_snapshot_sha256,'a'.repeat(64));assert.equal('absent_fields' in payload,false);
  assert.throws(()=>ui.makePayload({...doc,snapshot_sha256:null},values,[],'','approved','x'),/review_version_unavailable/);
  assert.equal(ui.stringValue({}),'');assert.equal(ui.stringValue({value:0}),'0');
});

test('editing an incorrect value and explicitly clearing it are literal corrections', () => {
  const source={...original,address:{value:'Синтетический адрес, дом 2'}};
  const edited={...values,address:''};
  const payload=ui.makePayload({...doc,original_fields:source},edited,['address'],'Пропущена квартира','rejected','stable');
  assert.deepEqual(payload.corrected_fields,{address:''});assert.deepEqual(payload.incorrect_fields,['address']);
  assert.equal(payload.comment,'Пропущена квартира');
  assert.throws(()=>ui.makePayload({...doc,original_fields:source},edited,[],'','approved','x'),/edited_card_requires_rejection/);
});

test('rejection can identify wrong ownership without changing a value, or use a general comment', () => {
  assert.deepEqual(ui.makePayload(doc,values,['inn'],'Принадлежит другому участнику','rejected','x').incorrect_fields,['inn']);
  assert.equal(ui.makePayload(doc,values,[],'Не тот участник','rejected','x').decision,'rejected');
  assert.throws(()=>ui.makePayload(doc,values,[],' ','rejected','x'),/rejection_reason_required/);
  assert.throws(()=>ui.makePayload(doc,values,[],'Ошибка участника','approved','x'),/edited_card_requires_rejection/);
});

test('normal typing, IME and repeated keys never submit; explicit chords work', () => {
  assert.equal(ui.shortcut({key:'Enter'}),null);assert.equal(ui.shortcut({key:'a'}),null);
  assert.equal(ui.shortcut({key:'Enter',ctrlKey:true,isComposing:true}),null);
  assert.equal(ui.shortcut({key:'Enter',ctrlKey:true,repeat:true}),null);
  assert.equal(ui.shortcut({key:'Enter',ctrlKey:true}),'approve');
  assert.equal(ui.shortcut({key:'Enter',metaKey:true,shiftKey:true}),'reject');
  assert.equal(ui.shortcut({key:'ArrowRight',altKey:true}),'skip');
});

test('original URLs cannot escape authenticated local document routes', () => {
  const base='http://127.0.0.1:8765/'; const fallback='/api/documents/synthetic/original';
  for(const unsafe of ['https://outside.invalid/document.pdf','javascript:alert(1)','//outside.invalid/x','/session?token=oops']) assert.equal(ui.safeOriginalURL(unsafe,'synthetic',base),fallback);
  assert.equal(ui.safeOriginalURL('/api/documents/synthetic/original','synthetic',base),fallback);
});

test('pending idempotent action payload is recognizable without discarding correction values', () => {
  const payload=ui.makePayload(doc,values,['inn'],'Причина','rejected','stable-action');
  const stored=JSON.parse(JSON.stringify({documentId:doc.id,payload}));
  assert.ok(ui.validPending(stored));assert.equal(stored.payload.action_id,'stable-action');
  assert.deepEqual(stored.payload,payload);assert.equal(ui.validPending(null),null);
});

test('source content is assigned through text APIs, with local assets and one viewer only', async () => {
  const script=await readFile(new URL('../sro_robot/research/static/review.js',import.meta.url),'utf8');
  const html=await readFile(new URL('../sro_robot/research/static/review.html',import.meta.url),'utf8');
  assert.doesNotMatch(script,/\.innerHTML\s*=/);assert.doesNotMatch(html,/https?:\/\//);
  assert.equal((html.match(/<iframe\b/g)||[]).length,1);assert.equal((html.match(/<img\b/g)||[]).length,1);
  assert.match(script,/sessionStorage\.setItem\(PENDING_KEY/);assert.match(script,/setInterval\(poll, 2000\)/);
});
