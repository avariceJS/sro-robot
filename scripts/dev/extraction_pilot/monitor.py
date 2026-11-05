"""Sample an owned pilot server tree and system swap without touching services."""
import argparse,json,os,subprocess,time,signal
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--pid',type=int,required=True);p.add_argument('--output',required=True);args=p.parse_args();alive=True

def stop(*_):
 global alive;alive=False
signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
with path.open('w') as stream:
 while alive:
  raw=subprocess.run(['ps','-axo','pid=,ppid=,rss=,%cpu='],capture_output=True,text=True).stdout
  rows=[x.split() for x in raw.splitlines() if len(x.split())==4];owned={args.pid}
  for _ in range(5):owned.update(int(pid) for pid,ppid,rss,cpu in rows if int(ppid) in owned)
  tree=[{'pid':int(pid),'rss_bytes':int(rss)*1024,'cpu_percent':float(cpu)} for pid,ppid,rss,cpu in rows if int(pid) in owned]
  if not tree:break
  swap=subprocess.run(['sysctl','vm.swapusage'],capture_output=True,text=True).stdout.strip()
  vm=subprocess.run(['vm_stat'],capture_output=True,text=True).stdout
  stream.write(json.dumps({'unix_time':time.time(),'tree':tree,'rss_sum_bytes':sum(r['rss_bytes'] for r in tree),'swap':swap,'vm_stat':vm})+'\n');stream.flush();time.sleep(2)
path.chmod(0o600)
