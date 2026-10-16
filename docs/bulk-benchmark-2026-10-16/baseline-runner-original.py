import hashlib, json, os, platform, statistics, subprocess, sys, time, uuid
from pathlib import Path
from urllib.request import Request, urlopen
from sqlalchemy import text
from itsdangerous import TimestampSigner
from base64 import b64encode
sys.path.insert(0, '/tmp/sro-bulk-benchmark-baseline/source/scripts/dev/nightly')
sys.path.insert(0, '/tmp/sro-bulk-benchmark-baseline/source')
import measure_http_batch as h
from app.db.base import SessionLocal, engine
from app.db.models import Task, TaskStatus
from app.services.auth import new_login_session, create_user, attach_external_session, SESSION_MAX_AGE_SECONDS
from app.db.models import User
from sqlalchemy import select
from redis import Redis
from rq import Queue, Worker

ROOT=Path('/tmp/sro-bulk-benchmark-baseline/source')
CORPUS=Path('/tmp/sro-bulk-benchmark-baseline/corpus')
OUT=Path('/tmp/sro-bulk-benchmark-baseline')
BASE='http://127.0.0.1:18081'
SECRET=os.environ['APP_SECRET_KEY']
manifest=json.loads((CORPUS/'manifest.json').read_text())
docs=manifest['documents']
# Create a genuine persisted app login session and sign the app's normal session payload.
with SessionLocal() as db:
    user=db.scalar(select(User).where(User.login=='synthetic-benchmark-operator'))
    if user is None: user=create_user(db,'synthetic-benchmark-operator','synthetic-benchmark-password','Synthetic Benchmark Operator','')
    token=__import__('secrets').token_urlsafe(32)
    row=new_login_session(db,user.id,token)
    synthetic_state=json.dumps({'cookies':[{'name':'synthetic','value':'benchmark-only','domain':'sroarbitr.sro-service.ru','path':'/','expires':-1,'httpOnly':True,'secure':True,'sameSite':'Lax'}],'origins':[]}).encode()
    attach_external_session(db,row,synthetic_state,SECRET,SESSION_MAX_AGE_SECONDS)
    db.commit()
cookie=TimestampSigner(SECRET).sign(b64encode(json.dumps({'user_id':user.id,'session_token':token}).encode())).decode()

def process_snapshot():
    with engine.connect() as c:
        r=c.execute(text("select xact_commit, tup_inserted, tup_updated, blks_read, blks_hit from pg_stat_database where datname=current_database()")).one()
        return [int(x) for x in r]

def poll_tasks(ids, pids, timeout=1200):
    start=time.monotonic(); deadline=start+timeout; samples=[]; maxrss=0; reads=[]
    while time.monotonic()<deadline:
        qstart=time.monotonic()
        with SessionLocal() as db:
            rows=db.scalars(select(Task).where(Task.id.in_(ids))).all()
            snap=[{'id':t.id,'status':t.status.value,'recognized':t.recognized or {},'created_at':t.created_at,'updated_at':t.updated_at,'error':bool(t.error)} for t in rows]
        reads.append(time.monotonic()-qstart)
        if len(snap)==len(ids) and all(x['status'] in ('needs_review','failed') for x in snap): break
        cpu,rss=h.process_sample(pids); samples.append(cpu); maxrss=max(maxrss,rss); time.sleep(.25)
    if len(snap)!=len(ids) or not all(x['status'] in ('needs_review','failed') for x in snap): raise TimeoutError(f'tasks did not complete: {len(snap)}/{len(ids)}')
    return snap, time.monotonic()-start, statistics.fmean(samples) if samples else 0, maxrss, reads

def post_chunks(selected, seed):
    times=[]; ids_before=h.current_task_max_id()
    for ix in range(0,len(selected),20):
        chunk=selected[ix:ix+20]
        body,ctype=h.multipart(chunk,CORPUS,hashlib.sha256(f'{seed}:{ix}'.encode()).hexdigest()[:48])
        req=Request(BASE+'/upload',data=body,method='POST',headers={'Content-Type':ctype,'Origin':BASE,'Accept':'text/html','Cookie':'session='+cookie})
        started=time.monotonic()
        with urlopen(req,timeout=300) as response: status=response.status
        times.append(time.monotonic()-started)
        if status!=200: raise RuntimeError(f'HTTP status {status}')
    with SessionLocal() as db:
        rows=db.scalars(select(Task).where(Task.id>ids_before).order_by(Task.id)).all()
        ids=[x.id for x in rows]
    if len(ids)!=len(selected): raise AssertionError(f'created {len(ids)} for {len(selected)} inputs')
    return times,ids

# Mix half text and half scanned inputs. Repeat selected bytes for each size/worker sweep;
# distinct idempotency keys make each upload a new synthetic operation.
text_docs=[d for d in docs if d['kind']=='text']
scan_docs=[d for d in docs if d['kind'].startswith('scan_')]
selected_by_n={}
for n in (10,20,50):
    half=n//2
    selected_by_n[n]=text_docs[:half]+scan_docs[:n-half]

results=[]
for n in (10,20,50):
  selected=selected_by_n[n]
  for wc in (1,2,4):
    if not h.queue_is_empty(): raise RuntimeError('queue not empty')
    procs=[]; handles=[]
    for i in range(wc):
      lp=OUT/'logs'/f'baseline-{n}-w{wc}-{i+1}.log'; lp.parent.mkdir(parents=True,exist_ok=True); f=lp.open('wb'); handles.append(f)
      p=subprocess.Popen([sys.executable,'scripts/dev/run_worker.py','ocr'],cwd=ROOT,env=os.environ.copy(),stdout=f,stderr=subprocess.STDOUT);procs.append(p)
    try:
      h.wait_for_workers(procs,wc)
      with SessionLocal() as db: before_id=int(db.scalar(select(__import__('sqlalchemy').func.coalesce(__import__('sqlalchemy').func.max(Task.id),0))) or 0)
      stats_before=process_snapshot(); started=time.monotonic()
      upload_times,ids=post_chunks(selected,f'baseline-{n}-{wc}-{uuid.uuid4()}')
      upload_wall=time.monotonic()-started
      pids=[p.pid for p in procs]
      task_rows,wait_wall,cpu,rss,db_reads=poll_tasks(ids,pids)
      total_wall=time.monotonic()-started
      stats_after=process_snapshot()
      actual_by_id={x['id']:x for x in task_rows}
      with SessionLocal() as db:
        loaded=db.scalars(select(Task).where(Task.id.in_(ids))).all()
        source_names={t.id:t.document.filename for t in loaded}
      field_names={'case_number':'case_number','doc_date':'doc_date','fio':'fio_raw','inn':'inn'}
      expected={d['filename']:d for d in selected}
      matches={k:0 for k in field_names}
      for task_id,row in actual_by_id.items():
        ex=expected[source_names[task_id]]; rec=row['recognized']
        for name,field in field_names.items():
          actual=(rec.get(field) or {}).get('value')
          matches[name]+=actual==ex[name]
      result={'count':n,'workers':wc,'input_kinds':{'text':sum(d['kind']=='text' for d in selected),'scan':sum(d['kind'].startswith('scan_') for d in selected)},'upload_requests':len(upload_times),'upload_request_seconds':upload_times,'upload_wall_seconds':upload_wall,'ocr_completion_after_upload_seconds':wait_wall,'end_to_end_seconds':total_wall,'docs_per_minute':n*60/total_wall,'task_p50_seconds':h.percentile([max(0,(r['updated_at']-r['created_at']).total_seconds()) for r in task_rows],50),'task_p95_seconds':h.percentile([max(0,(r['updated_at']-r['created_at']).total_seconds()) for r in task_rows],95),'task_read_query_mean_ms':statistics.fmean(db_reads)*1000,'task_read_query_p95_ms':h.percentile([x*1000 for x in db_reads],95),'sampled_worker_cpu_pct_total':cpu,'peak_worker_rss_mib_total':rss/1024/1024,'db_stat_delta':dict(zip(['xact_commit','tup_inserted','tup_updated','blks_read','blks_hit'],[b-a for a,b in zip(stats_before,stats_after)])),'terminal':{'needs_review':sum(x['status']=='needs_review' for x in task_rows),'failed':sum(x['status']=='failed' for x in task_rows)},'field_matches':matches,'field_total':n*4,'created_task_count':len(ids)}
      results.append(result)
      print(json.dumps(result,ensure_ascii=False),flush=True)
    finally:
      h.stop_workers(procs)
      for f in handles: f.close()
summary={'revision':'8c46c9ac9b8a8009246b92f43bbad2b45142de4','host':platform.platform(),'python':platform.python_version(),'corpus_manifest_sha256':hashlib.sha256((CORPUS/'manifest.json').read_bytes()).hexdigest(),'generator_sha256':hashlib.sha256((ROOT/'scripts/dev/nightly/generate_corpus.py').read_bytes()).hexdigest(),'results':results}
(OUT/'baseline.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print('SUMMARY='+json.dumps({k:v for k,v in summary.items() if k!='results'}|{'runs':len(results)},ensure_ascii=False))
