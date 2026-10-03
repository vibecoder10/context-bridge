"""Install a private runtime snapshot outside the worker's editable project."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import time
from manage_named_listener import definition
from relay import private_json

ROOT=Path(__file__).resolve().parent
def install(config,engine):
 config=Path(config).expanduser().resolve();runtime=config.with_suffix('.runtime')
 status=json.loads((runtime/'status.json').read_text());pid=status['pid']
 stamp=status.get('checked_at',status.get('last_poll',0))
 if status.get('current_job_id') or status.get('state') not in ('running','listening') or time.time()-stamp>25 or config.with_suffix('.work.json').exists() or config.with_suffix('.pending.json').exists():raise RuntimeError('The exact listener must finish its current work before installing.')
 label,spec=definition(config,engine);domain='gui/'+str(os.getuid());target=domain+'/'+label
 plist=Path.home()/'Library/LaunchAgents'/(label+'.plist');old=plistlib.loads(plist.read_bytes())
 snapshot=runtime/'worker-code';script=snapshot/('named_'+engine+'_listener.py')
 if old['ProgramArguments'] not in (spec['ProgramArguments'],[spec['ProgramArguments'][0],str(script),'--config',str(config)]):raise RuntimeError('Service is not the selected connection; preserve it.')
 actual=subprocess.check_output(['ps','-p',str(pid),'-o','args='],text=True)
 if str(config) not in actual or 'named_'+engine+'_listener.py' not in actual:raise RuntimeError('Listener identity mismatch.')
 os.kill(pid,signal.SIGTERM);deadline=time.monotonic()+25
 while subprocess.run(['ps','-p',str(pid)],capture_output=True).returncode==0:
  if time.monotonic()>deadline:raise RuntimeError('Wait for graceful shutdown; no duplicate was started.')
  time.sleep(.2)
 snapshot.mkdir(mode=0o700,exist_ok=True)
 files={}
 source_root=ROOT/'plugins/context-bridge/runtime'
 if not source_root.is_dir():source_root=ROOT
 for source in source_root.glob('*.py'):
  destination=snapshot/source.name;temporary=snapshot/(source.name+'.install')
  shutil.copyfile(source,temporary);temporary.chmod(0o400);os.replace(temporary,destination);files[source.name]=hashlib.sha256(destination.read_bytes()).hexdigest()
 spec['ProgramArguments']=[spec['ProgramArguments'][0],str(script),'--config',str(config)]
 spec['WorkingDirectory']=str(snapshot)
 plist.write_bytes(plistlib.dumps(spec));plist.chmod(0o600)
 subprocess.run(['launchctl','bootout',target],capture_output=True,check=True)
 subprocess.run(['launchctl','bootstrap',domain,str(plist)],capture_output=True,check=True)
 receipt={'engine':engine,'installed':True,'snapshot_outside_editable_project':True,'files':files}
 private_json(runtime/'installed-runtime.json',receipt)
 return {k:v for k,v in receipt.items() if k!='files'}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--engine',choices=['codex','claude'],required=True);a=p.parse_args();print(json.dumps(install(a.config,a.engine)))
