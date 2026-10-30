"""Compile the exported C# ingress and test startup boundaries without Unity."""
import argparse
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
PROJECT = HERE / 'StartupProbe.csproj'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--installed-project', type=Path,
                    help='Optional existing installer fixture: run its selected Python/server with --help only')
parser.add_argument('--output', type=Path, help='New report path (refuses overwrite)')
args = parser.parse_args()
if args.output and args.output.exists():
    parser.error('Choose a new report path')
subprocess.run(['dotnet', 'build', str(PROJECT), '--nologo', '--verbosity', 'quiet'], check=True)
cases = ['offline', 'capabilities', 'online', 'wrong-workspace', 'missing-python', 'missing-server']
if args.installed_project:
    cases.append('installed-help')
results = []
for case in cases:
    command = ['dotnet', 'run', '--no-build', '--project', str(PROJECT), '--', case]
    if case == 'installed-help':
        command.append(str(args.installed_project.resolve()))
    result = subprocess.run(command, capture_output=True, text=True, timeout=45)
    # Full failures stay in local terminal output. The public summary has no paths.
    print((result.stdout + result.stderr).strip(), flush=True)
    results.append({'case': case, 'exit_code': result.returncode})
report = {
    'scope': 'Actual exported C# Workflow method compiled under .NET 8 with Unity API stubs and fake HTTP/process-start boundaries. Not a Unity Editor test.',
    'installed_help_scope': 'When selected, the captured ProcessStartInfo runs the real installed Python/server with --help appended; no server listener or worker is started.',
    'passed': all(result['exit_code'] == 0 for result in results), 'results': results,
}
if args.output:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')
raise SystemExit(0 if report['passed'] else 1)
