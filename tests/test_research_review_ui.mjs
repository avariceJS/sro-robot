import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFile} from 'node:fs/promises';
const require = createRequire(import.meta.url);
const ui = require('../sro_robot/research/static/review.js');
const original = Object.fromEntries(ui.FIELDS.map(([key]) => [key, {value:key==='entity_kind'?'person':null}]));
const values = Object.fromEntries(ui.FIELDS.map(([key]) => [key, key==='entity_kind'?'person':'']));
const doc = {id:'synthetic-document',revision:3,snapshot_sha256:'a'.repeat(64),original_fields:original};

test('all20 review fields remain separate; empty approval does not invent absence', () => {
  assert.equal(ui.FIELDS.length,20); assert.equal(new Set(ui.FIELDS.map(([key])=>key)).size,20);
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

test('typed scopes show debtor fields for one canonical kind and keep common act fields', () => {
  const person=ui.visibleFields('person'), legal=ui.visibleFields('legal_entity'), ip=ui.visibleFields('sole_proprietor');
  for(const scope of [person,legal,ip]) for(const name of ['entity_kind',...ui.COMMON_FIELDS]) assert.ok(scope.includes(name));
  assert.ok(person.includes('last_name')&&person.includes('first_name')&&person.includes('middle_name')&&person.includes('snils'));assert.ok(!person.includes('fio_raw'));assert.equal(person.length,12);assert.ok(!person.includes('legal_name')&&!person.includes('ogrn'));
  assert.ok(legal.includes('legal_name')&&legal.includes('kpp')&&legal.includes('ogrn'));assert.ok(!legal.includes('fio_raw')&&!legal.includes('last_name')&&!legal.includes('snils')&&!legal.includes('ogrnip'));
  assert.ok(ip.includes('ogrnip')&&ip.includes('debtor_name'));assert.ok(!ip.includes('kpp')&&!ip.includes('ogrn')&&!ip.includes('fio_raw')&&!ip.includes('snils'));assert.equal(ip.length,10);
  assert.equal(ui.visibleFields('unknown').length,5);assert.equal(ui.visibleFields('unknown',true).length,20);
});

test('changing the visible kind never erases hidden drafts or silently approves them', () => {
  const draft={...values,last_name:'Synthetic person draft',legal_name:'Synthetic organization draft'};
  const before=JSON.stringify(draft);
  ui.visibleFields('legal_entity');ui.visibleFields('person');
  assert.equal(JSON.stringify(draft),before);
  assert.deepEqual(ui.correctedFields(original,draft),{last_name:'Synthetic person draft',legal_name:'Synthetic organization draft'});
  assert.throws(()=>ui.makePayload(doc,draft,[],'','approved','x'),/edited_card_requires_rejection/);
  assert.throws(()=>ui.makePayload(doc,draft,[],'Synthetic type correction','rejected','x'),/hidden_corrections_require_expansion/);
  const payload=ui.makePayload(doc,draft,[],'Synthetic type correction','rejected','x',{showAll:true});
  assert.equal(payload.reviewed_fields.length,20);assert.equal(payload.corrected_fields.legal_name,'Synthetic organization draft');
});

test('approval declares only visible scope; unknown needs explicit manual expansion', () => {
  const legalOriginal={...original,entity_kind:{value:'legal_entity'},fio_raw:{value:'Hidden unrelated person'}};
  const legalValues={...values,entity_kind:'legal_entity',fio_raw:'Hidden unrelated person'};
  const payload=ui.makePayload({...doc,fields:legalOriginal},legalValues,[],'','approved','x');
  assert.ok(!payload.reviewed_fields.includes('fio_raw'));assert.ok(!payload.reviewed_fields.includes('snils'));
  const unknown={...doc,fields:{...original,entity_kind:{value:null}}};const unknownValues={...values,entity_kind:'unknown'};
  assert.throws(()=>ui.makePayload(unknown,unknownValues,[],'','approved','x'),/entity_kind_required/);
  assert.equal(ui.makePayload(unknown,unknownValues,[],'','approved','x',{showAll:true}).reviewed_fields.length,20);
  assert.equal(ui.correctedFields(unknown.fields,unknownValues).entity_kind,undefined);
  assert.equal(ui.correctedFields(original,unknownValues).entity_kind,null);
});

test('corrections compare the displayed projection while immutable original remains separate', () => {
  const fields={...original,address:{value:'Synthetic projected address'}};
  const projected={...doc,review_projection:{fields},original_fields:{...original,address:{value:null}}};
  const payload=ui.makePayload(projected,{...values,address:'Synthetic projected address'},[],'','approved','x');
  assert.deepEqual(payload.corrected_fields,{});assert.equal(projected.original_fields.address.value,null);
});

test('explicit candidate substitution is one coherent bundle and clears competing identity fields', () => {
  const previous={...values,entity_kind:'legal_entity',legal_name:'Old company',inn:'old-company-id',kpp:'old-kpp',case_number:'SYNTHETIC-CASE'};
  const candidate={id:'synthetic-candidate',entity_kind:'person',fields:{last_name:{value:'Synthetic'},first_name:{value:'Person'},middle_name:{value:'Example'},fio_raw:{value:'Synthetic Person Example'},inn:{value:'person-id'}}};
  const result=ui.candidateDraft(candidate,previous);
  assert.equal(result.fio_raw,'Synthetic Person Example');assert.equal(result.last_name,'Synthetic');assert.equal(result.first_name,'Person');assert.equal(result.inn,'person-id');assert.equal(result.entity_kind,'person');
  assert.equal(result.legal_name,'');assert.equal(result.kpp,'');assert.equal(result.case_number,'SYNTHETIC-CASE');
  assert.equal(previous.kpp,'old-kpp');
  const payload=ui.makePayload({...doc,fields:Object.fromEntries(Object.entries(previous).map(([key,value])=>[key,{value}]))},result,[],'Selected one source occurrence','rejected','x',{showAll:true,selectedCandidateId:candidate.id});
  assert.equal(payload.selected_candidate_id,'synthetic-candidate');assert.equal(payload.corrected_fields.kpp,'');
});

test('legacy pending actions are retained for exact server replay rather than silently discarded', () => {
  const payload={action_id:'legacy-action',expected_revision:1,decision:'rejected',corrected_fields:{address:''},incorrect_fields:['address'],comment:'Synthetic legacy reason'};
  assert.ok(ui.validPending({documentId:'synthetic',payload}));assert.equal(payload.corrected_fields.address,'');
});

test('civil party roles are shown explicitly and never inferred as debtor from organization IDs', () => {
  const bare={fields:{...original,entity_kind:{value:'legal_entity'},ogrn:{value:'synthetic-registration-id'}}};
  assert.equal(ui.subjectInfo(bare).isDebtor,false);assert.equal(ui.subjectInfo(bare).label,'Участник не выбран');
  const defendant={...bare,review_projection:{subject_role:'defendant',subject_is_debtor:false,fields:bare.fields}};
  const subject=ui.subjectInfo(defendant);assert.equal(subject.label,'Ответчик');assert.equal(subject.isDebtor,false);
  assert.equal(ui.fieldTitle('legal_name',subject),'Полное наименование ответчика');assert.equal(ui.fieldTitle('entity_kind',subject),'Тип участника');
  const debtor=ui.subjectInfo({...bare,review_projection:{subject_role:'debtor',subject_is_debtor:true}});
  assert.equal(debtor.isDebtor,true);assert.equal(ui.fieldTitle('last_name',debtor),'Фамилия должника');assert.equal(ui.fieldTitle('fio_raw',debtor),'Исходное ФИО из документа');
});

test('explicit coherent party selection carries its own role rather than the initial subject role', () => {
  const doc={review_projection:{subject_role:'defendant',candidates:[{id:'synthetic-plaintiff',role:'plaintiff',role_location:{source_page:2},role_warning:'Synthetic role requires review'}]}};
  const chosen=ui.subjectInfo(doc,'synthetic-plaintiff');
  assert.equal(chosen.role,'plaintiff');assert.equal(chosen.label,'Истец');assert.equal(chosen.isDebtor,false);
  assert.equal(chosen.roleLocation.source_page,2);assert.equal(ui.fieldTitle('last_name',chosen),'Фамилия истца');
  assert.equal(ui.fieldTitle('short_name',chosen),'Сокращённое наименование из документа');
});


test('a stale manual subject ID does not borrow the new projection participant role', () => {
  const current={review_projection:{subject_role:'defendant',selected_candidate_id:'new-candidate',candidates:[]}};
  const stale=ui.subjectInfo(current,'old-candidate');
  assert.equal(stale.role,null);assert.equal(stale.selected,false);assert.equal(stale.label,'Участник не выбран');
  assert.match(stale.warning,/другой версии/);
});


test('person component corrections stay editable in primary scope without silently rewriting raw FIO in client', () => {
  const fields={...original,last_name:{value:'Original'},first_name:{value:'Person'},middle_name:{value:'Example'},fio_raw:{value:'Original Person Example'}};
  const shown={...values,last_name:'Changed',first_name:'Person',middle_name:'Example',fio_raw:'Original Person Example'};
  const payload=ui.makePayload({...doc,fields},shown,['last_name'],'Synthetic surname correction','rejected','x');
  assert.deepEqual(payload.corrected_fields,{last_name:'Changed'});
  assert.ok(payload.reviewed_fields.includes('last_name'));assert.ok(!payload.reviewed_fields.includes('fio_raw'));
  assert.equal(payload.reviewed_fields.length,12);
});


test('a typed but ambiguous participant cannot be approved as an empty selected card', () => {
  const ambiguous={...doc,review_projection:{fields:original,entity_kind:'person',selection_required:true,candidates:[]}};
  assert.throws(()=>ui.makePayload(ambiguous,values,[],'','approved','x'),/subject_selection_required/);
  const rejection=ui.makePayload(ambiguous,values,[],'Need local subject selection','rejected','x');
  assert.equal(rejection.decision,'rejected');
});
