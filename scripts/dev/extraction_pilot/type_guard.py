"""Diagnostic only: withhold inferred actual type after a generic OCR address label.

Uses B's actual source spans and text; no gold/model answer chooses the label.
It neither edits application rules nor fills a new address/type value.
"""
import argparse,json,re
from pathlib import Path
LABEL=re.compile(r'\b(адрес(?:\s+(?:проживания|регистрации))?|фактическ\w*\s+адрес|почтов\w*\s+адрес)\s*:',re.I)
def guard(row):
 changes=[];rows={r['attr']:r for r in row.get('review',[])};kind=rows.get('address_kind');address=rows.get('address')
 if not kind or not address or kind.get('value')!='actual':return changes
 start=address.get('source_start');text=re.sub(r'[ \t]+',' ','\n'.join(p['text'] for p in row['pages']).strip().replace('\u00a0',' '))
 if not isinstance(start,int):return changes
 labels=list(LABEL.finditer(text[max(0,start-180):start]))
 if labels and labels[-1].group(1).casefold()=='адрес':
  kind['value']='';kind['warning']='Diagnostic: generic address label does not establish actual address type'
  changes.append({'attr':'address_kind','from':'actual','to':'unknown_abstention','basis':'nearest labeled address in 180-character source window is generic; no gold used'})
 return changes
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--prepared',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True);args.output.chmod(0o700);records=[]
 for file in sorted(args.prepared.glob('*.json')):
  row=json.loads(file.read_text());changes=guard(row)
  if changes:records.append({'source_id':row['source_id'],'changes':changes})
  (args.output/file.name).write_text(json.dumps(row,ensure_ascii=False,indent=2)+'\n');(args.output/file.name).chmod(0o600)
 (args.output/'guard-summary.json').write_text(json.dumps({'changes':records,'scope':'diagnostic B review projection only; no model or gold used'},indent=2)+'\n')
