"""Install the pinned Mac arm64 pilot runtime and exactly two model artifacts.

No server is launched. Downloads contain public binaries/weights only. Existing
files must match their pinned checksum; mismatches are never overwritten.
"""
import argparse,hashlib,json,platform,subprocess,tarfile,urllib.request
from pathlib import Path
REPO=Path(__file__).resolve().parents[3]
REV='e87f176479d0855a907a41277aca2f8ee7a09523'
ARTIFACTS=[
 ('models/Qwen3.5-4B-Q4_K_M.gguf',f'https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/{REV}/Qwen3.5-4B-Q4_K_M.gguf',2740937888,'00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4'),
 ('models/mmproj-F16.gguf',f'https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/{REV}/mmproj-F16.gguf',672423616,'cd88edcf8d031894960bb0c9c5b9b7e1fea6ebee02b9f7ce925a00d12891f864'),
 ('runtime/llama-b12128-bin-macos-arm64.tar.gz','https://github.com/ggml-org/llama.cpp/releases/download/b12128/llama-b12128-bin-macos-arm64.tar.gz',16672024,'b77c6b4c4a0217153a14cfc062b341622a07ec5726c8d302386d75749780be25'),
]
def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
 return h.hexdigest()
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=REPO/'.local-run/extraction-pilot-20261105');p.add_argument('--verify-only',action='store_true');args=p.parse_args();args.root.mkdir(parents=True,exist_ok=True);args.root.chmod(0o700);verified=[]
 for relative,url,size,expected in ARTIFACTS:
  target=args.root/relative
  if not target.exists():
   if args.verify_only:raise RuntimeError('Missing artifact: '+relative)
   target.parent.mkdir(parents=True,exist_ok=True);temp=target.with_suffix(target.suffix+'.download')
   if temp.exists():raise RuntimeError('Prior incomplete download retained: '+str(temp))
   with urllib.request.urlopen(url,timeout=120) as response,temp.open('xb') as out:
    for part in iter(lambda:response.read(1024*1024),b''):out.write(part)
   if temp.stat().st_size!=size or digest(temp)!=expected:raise RuntimeError('Downloaded artifact checksum mismatch; retained '+str(temp))
   temp.replace(target)
  if target.stat().st_size!=size or digest(target)!=expected:raise RuntimeError('Existing artifact checksum mismatch: '+str(target))
  verified.append({'path':relative,'url':url,'bytes':size,'sha256':expected})
 runtime=args.root/'runtime/llama-b12128/llama-server'
 if not runtime.exists() and not args.verify_only:
  if platform.system()!='Darwin' or platform.machine()!='arm64':raise RuntimeError('Pinned binary requires macOS arm64')
  with tarfile.open(args.root/ARTIFACTS[-1][0]) as archive:archive.extractall(args.root/'runtime',filter='data')
 print(json.dumps({'verified':verified,'runtime_exists':runtime.exists()},indent=2))
if __name__=='__main__':main()
