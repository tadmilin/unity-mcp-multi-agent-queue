"""Run isolated queue tests; does not start Unity or connect to the live MCP ports."""
import argparse,json,os,re,subprocess,sys,time
from pathlib import Path

HERE=Path(__file__).resolve().parent
PROJECT=HERE/'UnityProject'
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--dependency-path',type=Path,help='Optional existing Python dependency directory; no install or modification')
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
if args.output.exists():parser.error('Choose a new report path; reports are not overwritten')
(PROJECT/'Tools/UnityWorkflowMcp/.scratch').mkdir(exist_ok=True)
bootstrap='import sys,site; d=sys.argv.pop(1); sys.path.insert(0,d) if d else None; site.addsitedir(d) if d else None; import unittest; unittest.main(module=None)'
groups=[('coordinator','UnityJobCoordinator',['discover','-p','test_*.py']),
        ('workflow','UnityWorkflowMcp',['discover','-p','test_*.py']),
        ('native_observer','UnityNativeObserver',['test_native_workflow_observer','test_process_watch','test_code_fingerprint'])]
results=[]
for label,folder,tests in groups:
 started=time.monotonic()
 r=subprocess.run([sys.executable,*(['-s'] if args.dependency_path else []),'-c',bootstrap,str(args.dependency_path.resolve()) if args.dependency_path else '',*tests],cwd=PROJECT/'Tools'/folder,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180,env={**os.environ,'PYTHONUTF8':'1','DO_NOT_TRACK':'1','FASTMCP_CHECK_FOR_UPDATES':'off'})
 log=r.stdout+r.stderr
 for path,replacement in [(str(HERE.parent),'<checkout>'),(str(args.dependency_path) if args.dependency_path else '', '<dependencies>')]:
  if path:log=log.replace(path,replacement).replace(path.replace('\\','/'),replacement)
 count=re.search(r'Ran (\d+) tests?',log)
 results.append(dict(suite=label,exit_code=r.returncode,tests=int(count[1]) if count else None,seconds=round(time.monotonic()-started,3),log=log))
 print(label,r.returncode,'tests',count[1] if count else 'unknown',flush=True)
for name in ['test_direct_client.mjs','test_observe_client.mjs','test_client_trace.mjs']:
 started=time.monotonic();r=subprocess.run(['node',name],cwd=PROJECT/'Tools/UnityWorkflowMcp',capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=45)
 results.append(dict(suite=name,exit_code=r.returncode,seconds=round(time.monotonic()-started,3),log=r.stdout+r.stderr))
 print(name,r.returncode,flush=True)
report=dict(scope='Offline integration/unit tests with temporary ledgers, fake authorities/HTTP and test-owned process lifetime. No Unity acceptance or fresh installation qualification.',dependency_source='existing external directory' if args.dependency_path else 'current interpreter',python=sys.version.split()[0],platform=sys.platform,passed=all(x['exit_code']==0 for x in results),results=results)
args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
raise SystemExit(0 if report['passed'] else 1)
