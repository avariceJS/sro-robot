"""Replay the existing explicit-copy INN proposal from saved A/B evidence.

No OCR, application service, database session or external operation is run.
The raw review-suggestion pool is unchanged; this records which of its proposals
is actually exposed by the current alternate_person_inn UI rule.
"""
import argparse,json,os,sys,time
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--results',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args();env=ROOT/'.local-run/extraction-pilot-20261105/empty.env';env.touch();env.chmod(0o600)
 os.environ.update(SRO_ROBOT_ENV_FILE=str(env),APP_DEBUG='1',APP_SECRET_KEY='synthetic-verification-session-key-0000000000000000',DATABASE_URL='postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/sro',REDIS_URL='redis://synthetic:synthetic@127.0.0.1:1/0',MIGRATION_DATABASE_URL='')
 from app.services.fields import ReviewField,alternate_person_inn
 records=[]
 for route in ('A','B'):
  for path in sorted((args.results/route).glob('*.json')):
   row=json.loads(path.read_text());fields=row['fields'];recognized={k:v for k,v in fields.items() if isinstance(v,dict) and 'value' in v};recognized['_review_suggestions_v1']=fields.get('review_suggestions',[])
   task=SimpleNamespace(scenario='entity_review' if 'legal_name' in fields else 'person_card',recognized=recognized,values={});rows=[ReviewField(**r) for r in row['review']];start=time.perf_counter();extra=alternate_person_inn(task,rows)
   records.append({'source_id':row['source_id'],'route':route,'alternate_inn':extra,'projection_seconds':time.perf_counter()-start,'basis':'same saved source evidence, actual existing UI predicate; explicit employee-copy proposal, not a main-field value'})
 args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps({'records':records},ensure_ascii=False,indent=2)+'\n');args.output.chmod(0o600)
if __name__=='__main__':main()
