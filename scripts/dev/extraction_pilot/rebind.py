"""Revalidate saved model evidence without a new inference request.

Only derived validation/evidence keys change. Raw model assertions and failures
remain in the preserved files; the manifest records before/after file hashes and
assertion hashes. Never pass original documents to a network destination.
"""
import argparse,copy,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from scripts.dev.extraction_pilot.harness import bind_model,checked_source,dump,formal,sha
DERIVED={'evidence','validation','normalization_support','owner_evidence','ownership_evidence'}
def assertions(value):
 if isinstance(value,dict):return {k:assertions(v) for k,v in value.items() if k not in DERIVED}
 if isinstance(value,list):return [assertions(v) for v in value]
 return value
def content_hash(value):return hashlib.sha256(json.dumps(assertions(value),ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def transfer_derived(target,source):
 """Copy only leaf-derived checks into an identical merged assertion."""
 if assertions(target)!=assertions(source):raise RuntimeError('Merged retry assertion differs from leaf')
 if isinstance(target,dict):
  for key in DERIVED:
   if key in source:target[key]=copy.deepcopy(source[key])
   else:target.pop(key,None)
  for key,value in target.items():
   if key not in DERIVED and isinstance(value,(dict,list)):transfer_derived(value,source[key])
 elif isinstance(target,list):
  for item,other in zip(target,source,strict=True):transfer_derived(item,other)

def bind_merged_parent(call):
 """A parent carries copied leaf assertions, never independently supplied text."""
 result=call.get('result')
 if not isinstance(result,dict):return
 children=[child.get('result') for child in call['retry_calls'] if isinstance(child.get('result'),dict)]
 parties=[party for child in children for party in child.get('participants',[])]
 for target,source in zip(result.get('participants',[]),parties,strict=True):transfer_derived(target,source)
 for key,target in result.get('document',{}).items():
  if not target.get('value'):
   target['evidence']=checked_source(target,{'page_id':call['page_id'],'lines':[]},'C')
   target['validation']=formal(key,target.get('value',''))
   continue
  source=next((child['document'][key] for child in children
               if key in child.get('document',{}) and assertions(child['document'][key])==assertions(target)),None)
  if source is None:raise RuntimeError('Merged document assertion differs from retry leaves')
  transfer_derived(target,source)

def qualify_visual_fragment(value):
 """Full-page OCR cannot prove a quote was visible in a retry image crop."""
 if isinstance(value,dict):
  if value.get('status')=='page_quote_ocr_corroborated' and value.get('granularity')=='page':
   value['status']='page_only_unverified_quote'
   value['note']='Quote matches full-page OCR; visibility in this retry image crop is unverified'
  for child in value.values():qualify_visual_fragment(child)
 elif isinstance(value,list):
  for child in value:qualify_visual_fragment(child)

def rebind(row,pages,route):
 def visit(call,page,visual_fragment=False):
  children=call.get('retry_calls') or []
  if children:
   if route=='C' or page.get('is_pdf'):
    lines=page['lines'];mid=len(lines)//2
    if mid<2 or len(children)!=2:raise RuntimeError('Unexpected saved text subdivision')
    selected=(lines[:mid+2],lines[max(0,mid-2):])
    for child,subset in zip(children,selected,strict=True):
     fragment=page|{'lines':subset}
     visit(child,fragment,visual_fragment)
   else:
    for child in children:visit(child,page,True)
   bind_merged_parent(call)
  elif 'result' in call:
   bind_model(call['result'],page,'C' if page.get('is_pdf') else route)
   if visual_fragment and route=='D' and not page.get('is_pdf'):
    qualify_visual_fragment(call['result'])
 for call in row.get('calls',[]):visit(call,pages[call['page_id']])
 return row
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--results',type=Path,required=True);p.add_argument('--prepared',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True);args=p.parse_args();records=[]
 for route in ('C','D'):
  for path in sorted((args.results/route).glob('*.json')):
   row=json.loads(path.read_text())
   if row.get('route')!=route or 'calls' not in row:continue
   before=sha(path);claim_hash=content_hash(row);reader=json.loads((args.prepared/path.name).read_text());pages={page['page_id']:page for page in reader['pages']}
   rebind(row,pages,route)
   if content_hash(row)!=claim_hash:raise RuntimeError('Raw model assertions changed')
   dump(path,row);records.append({'route':route,'source_id':row['source_id'],'before_sha256':before,'after_sha256':sha(path),'assertions_sha256_unchanged':claim_hash})
 dump(args.manifest,{'records':records,'model_requests':0,'harness_sha256':sha(Path(__file__).with_name('harness.py'))})
