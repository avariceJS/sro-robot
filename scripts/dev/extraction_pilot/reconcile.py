"""Keep model occurrences, conflicts and coverage without gold-assisted merging.

This creates an experimental review projection only. Equal names create candidate
links, never an authoritative business identity. No database writes exist here.
"""
import argparse,hashlib,json,re
from pathlib import Path

def canonical(value):return re.sub(r'[^а-яёa-z0-9]','',str(value or '').casefold())

def leaf_calls(call,path):
    """A subdivided parent's merged result can omit leaf alternatives."""
    children=call.get('retry_calls') or []
    if children:
        for index,child in enumerate(children):
            yield from leaf_calls(child,path+[index])
    else:
        yield path,call

def discarded_retries(call,path):
    children=call.get('retry_calls') or []
    if children:
        initial=call.get('initial_attempt') or {}
        yield {'page_id':call.get('page_id'),'call_path':path,'error':initial.get('error'),'finish_reason':initial.get('finish_reason'),'result_discarded':True}
        for index,child in enumerate(children):
            yield from discarded_retries(child,path+[index])

def reconcile(row):
    occurrences=[];coverage=[];metadata={};links=[];conflicts=[];discarded_attempts=[];parent_incomplete=False
    for ci,call in enumerate(row.get('calls',[])):
        discarded_attempts.extend(discarded_retries(call,[ci]))
        if call.get('retry_calls'):parent_incomplete |= bool(call.get('incomplete') or call.get('error'))
        for path,leaf in leaf_calls(call,[ci]):
            page_id=leaf.get('page_id',call.get('page_id'));incomplete=bool(leaf.get('incomplete') or leaf.get('error'))
            coverage.append({'page_id':page_id,'call_path':path,'state':'incomplete' if incomplete else 'processed_provided_fragment','error':leaf.get('error')})
            payload=leaf.get('result') or {}
            for key,atom in payload.get('document',{}).items():
                if isinstance(atom,dict) and (atom.get('value') or atom.get('raw')):
                    metadata.setdefault(key,[]).append(atom|{'page_id':page_id,'call_path':path,'state':'incomplete_fragment' if incomplete else 'review_only'})
            for pi,party in enumerate(payload.get('participants',[])):
                entry={'occurrence_id':f"{row['source_id']}:{'.'.join(map(str,path))}:{pi}",'page_id':page_id,'call_path':path,'party':party,'state':'incomplete_fragment' if incomplete else 'review_only'}
                name=canonical(party.get('name',{}).get('value') or party.get('name',{}).get('raw'))
                for earlier in occurrences:
                    other=earlier['party'];other_name=canonical(other.get('name',{}).get('value') or other.get('name',{}).get('raw'))
                    if name and name==other_name:
                        links.append({'from':earlier['occurrence_id'],'to':entry['occurrence_id'],'kind':'same_name_candidate_only','confirmed_identity':False})
                occurrences.append(entry)
    for key,atoms in metadata.items():
        if len({canonical(a.get('value') or a.get('raw')) for a in atoms})>1:conflicts.append({'kind':'document_field_conflict','field':key,'alternatives':atoms})
    by_name={}
    for o in occurrences:
        n=canonical(o['party'].get('name',{}).get('value') or o['party'].get('name',{}).get('raw'))
        for r in o['party'].get('roles',[]):by_name.setdefault((n,r['role']),set()).add(r['status'])
    for (name,role),statuses in by_name.items():
        if name and len(statuses)>1:conflicts.append({'kind':'role_status_needs_event_reconciliation','name_key_sha256':hashlib.sha256(name.encode()).hexdigest(),'role':role,'statuses':sorted(statuses)})
    return {'source_id':row['source_id'],'route':row['route'],'scope':'provided pages only, not an assertion of complete act or source absence','coverage':coverage,'incomplete_coverage':parent_incomplete or not coverage or any(c['state']=='incomplete' for c in coverage),'discarded_initial_attempts':discarded_attempts,'participants':occurrences,'candidate_identity_links':links,'document_candidates':metadata,'conflicts':conflicts,'requires_review':True,'absence_inference_permitted':False,'address_type_pairing':'Multiple addresses/types stay unpaired alternatives unless explicitly linked by a source occurrence; no positional pairing is assumed','gold_used':False}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',required=True);p.add_argument('--output',required=True);args=p.parse_args();out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(reconcile(json.loads(Path(args.input).read_text())),ensure_ascii=False,indent=2)+'\n');out.chmod(0o600)
