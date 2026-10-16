import hashlib,json,platform,sys,time
from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
ROOT=Path('/tmp/sro-bulk-benchmark-baseline/source')
CORPUS=Path('/tmp/sro-bulk-benchmark-baseline/corpus')
OUT=Path('/tmp/sro-bulk-benchmark-baseline/dpi')
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'scripts/dev/nightly'))
from generate_corpus import records,scan_image
from sro_robot.core.extract import parse_file
font=Path('/System/Library/Fonts/Supplemental/Arial.ttf')
pdfmetrics.registerFont(TTFont('BenchmarkCyrillic',str(font)))
by_name={x['filename']:x for x in records()}
selected=[by_name[f'synthetic-{i:03d}.pdf'] for i in (91,92,93,94,81,82,83,71,72,73)]
quality_for={91:'low',92:'low',93:'low',94:'low',81:'medium',82:'medium',83:'medium',71:'high',72:'high',73:'high'}
OUT.mkdir(parents=True,exist_ok=True)
items=[]
for r in selected:
 q=quality_for[int(str(r['filename']).split('-')[1].split('.')[0])]
 path=OUT/str(r['filename'])
 doc=canvas.Canvas(str(path),pagesize=letter,invariant=1,pageCompression=1)
 for _page in range(2):
  im=scan_image(r,font,q)
  doc.drawImage(ImageReader(im),0,0,width=letter[0],height=letter[1],preserveAspectRatio=False)
  doc.showPage()
 doc.save()
 items.append({**r,'kind':f'multipage_scan_{q}','pages':2,'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
manifest={'generator':'synthetic two-page scan wrapper over nightly corpus','source_manifest_sha256':hashlib.sha256((CORPUS/'manifest.json').read_bytes()).hexdigest(),'font_sha256':hashlib.sha256(font.read_bytes()).hexdigest(),'documents':items}
(OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
results=[]
for d in items:
 path=OUT/d['filename']; expected={'fio':d['fio'],'inn':d['inn'],'doc_date':d['doc_date'],'address':'101000, вымышленный город Макетный, улица Учебная, дом 1'}
 by_dpi={}
 for dpi in (300,200):
  started=time.monotonic(); card=parse_file(path,dpi=dpi); elapsed=time.monotonic()-started
  actual={'fio':card.fio_raw.value or card.fio,'inn':card.inn.value,'doc_date':card.doc_date.value,'address':card.address.value}
  by_dpi[str(dpi)]={'seconds':elapsed,'actual':actual,'present':{k:bool(v) for k,v in actual.items()},'exact':{k:actual[k]==expected[k] for k in expected}}
 results.append({'filename':d['filename'],'kind':d['kind'],'pages':2,'expected':expected,'by_dpi':by_dpi})
 print(json.dumps(results[-1],ensure_ascii=False),flush=True)
summary={'source_revision':'8c46c9ac9b8a8009246b92f43bbad2b45142de4','host':platform.platform(),'python':platform.python_version(),'source_manifest_sha256':manifest['source_manifest_sha256'],'dpi_manifest_sha256':hashlib.sha256((OUT/'manifest.json').read_bytes()).hexdigest(),'documents':len(items),'results':results}
for dpi in (300,200):
 key=str(dpi)
 summary[f'aggregate_{dpi}']={'wall_seconds':sum(x['by_dpi'][key]['seconds'] for x in results),'present':{field:sum(x['by_dpi'][key]['present'][field] for x in results) for field in ('fio','inn','doc_date','address')},'exact':{field:sum(x['by_dpi'][key]['exact'][field] for x in results) for field in ('fio','inn','doc_date','address')},'exact_documents':sum(all(x['by_dpi'][key]['exact'].values()) for x in results)}
(OUT/'dpi-comparison.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print('SUMMARY='+json.dumps({k:v for k,v in summary.items() if k!='results'},ensure_ascii=False))
