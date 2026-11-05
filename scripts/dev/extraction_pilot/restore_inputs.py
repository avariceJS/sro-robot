"""Reconstruct diagnostic input fragments from immutable pages and saved trees.

Images are deterministic reconstructions, not a claim that their request digests
were measured at inference time. Crop geometry and the recipe are preserved.
No model call is made. Saved assertions are never changed.
"""
import argparse,hashlib,json,sys
from pathlib import Path
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from scripts.dev.extraction_pilot.harness import image_path,dump,sha

def restore(row,reader,out,route):
 pages={p['page_id']:p for p in reader['pages']};records=[]
 def visit(call,page,node,source_image,offset=(0,0)):
  rec={'route':route,'source_id':row['source_id'],'node':node,'page_id':call['page_id'],'kind':'deterministically_reconstructed_not_original_request_capture'}
  if source_image:
   rec.update(input_image_path=str(source_image),input_image_sha256=sha(source_image),image_offset_in_logical_page=list(offset))
  else:
   lines=page['lines'];rec.update(first_line_id=lines[0]['id'] if lines else None,last_line_id=lines[-1]['id'] if lines else None,line_count=len(lines))
  records.append(rec)
  children=call.get('retry_calls',[])
  if not children:return
  if route=='C' or page.get('is_pdf'):
   lines=page['lines'];mid=len(lines)//2
   for i,selected in enumerate((lines[:mid+2],lines[max(0,mid-2):])):
    fragment=dict(page);fragment['lines']=selected;visit(children[i],fragment,node+f'-{i}',None)
  else:
   im=Image.open(source_image).convert('RGB');w,h=im.size;mid=h//2
   for i,box in enumerate(((0,0,w,min(h,mid+60)),(0,max(0,mid-60),w,h))):
    target=out/f"{row['source_id']}-{node}-{i}.jpg";target.parent.mkdir(parents=True,exist_ok=True);im.crop(box).save(target,quality=95)
    visit(children[i],page,node+f'-{i}',target,(offset[0]+box[0],offset[1]+box[1]))
   im.close()
 for i,call in enumerate(row.get('calls',[])):
  page=pages[call['page_id']];visit(call,page,f'p{i+1}',image_path(page['image_path']) if route=='D' and not page.get('is_pdf') else None)
 return records
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--results',type=Path,required=True);p.add_argument('--prepared',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args();records=[]
 for route in ('C','D'):
  for file in sorted((args.results/route).glob('*.json')):
   row=json.loads(file.read_text())
   if row.get('route')==route and 'calls' in row:records+=restore(row,json.loads((args.prepared/file.name).read_text()),args.output/route,route)
 dump(args.output/'manifest.json',{'model_requests':0,'records':records,'interpretation':'Reconstruction from frozen source hashes/call tree, not measured historical wire-image hashes'})
