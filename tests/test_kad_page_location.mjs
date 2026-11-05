import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';
const source = await readFile(new URL('../app/web/static/kad-page-location.js', import.meta.url), 'utf8');
function harness(hash = '#kad-search', readyState = 'loading') {
  const events = new Map(); const scrolls = []; const replacements = [];
  const location = {pathname:'/debtors/intake',search:'?registry=persons',hash};
  const state = {staffContext:'preserved'};
  const document = {readyState, getElementById(id) {return ['kad-search','document-upload','main-content'].includes(id) ? {scrollIntoView(){scrolls.push(id);}} : null;}, addEventListener(name,callback){events.set(name,callback);}};
  vm.runInNewContext(source,{location,document,history:{state,replaceState(s,_,url){replacements.push({state:s,url});location.hash='';}},window:{addEventListener(name,callback){events.set(name,callback);}}});
  return {events,scrolls,replacements,location,state};
}
test('normalizes before extension idle while preserving registry and history state',()=>{
 const h=harness();assert.equal(h.location.hash,'');assert.deepEqual(h.replacements,[{state:h.state,url:'/debtors/intake?registry=persons'}]);assert.deepEqual(h.scrolls,[]);h.events.get('DOMContentLoaded')();assert.deepEqual(h.scrolls,['kad-search']);
});
test('later upload and accessibility anchors keep scrolling without retaining a hash',()=>{
 const h=harness('', 'complete');for(const id of ['document-upload','main-content']){h.location.hash='#'+id;h.events.get('hashchange')();assert.equal(h.location.hash,'');}assert.deepEqual(h.scrolls,['document-upload','main-content']);
});
test('no fragment leaves history untouched; unknown fragment does not throw',()=>{
 const h=harness('', 'complete');assert.equal(h.replacements.length,0);h.location.hash='#unknown';h.events.get('hashchange')();assert.equal(h.replacements.length,1);assert.deepEqual(h.scrolls,[]);
});
