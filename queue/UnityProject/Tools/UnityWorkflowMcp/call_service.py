"""Call the workspace MCP service once when Unity ingress is temporarily busy.

This is a real SDK client fallback, not native agent acceptance or a status loop.
The service owns waiting/execution. Use the same key for outcome recovery.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time

import server  # Load the pinned workspace runtime without starting a server.
from fastmcp import Client


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tool', required=True, choices=('unity_job_run', 'unity_job_result', 'unity_phase_run', 'unity_phase_result'))
    parser.add_argument('--key', required=True)
    parser.add_argument('--manifest')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    parameters = {'submission_key': args.key, 'wait_seconds': 1800}
    if args.manifest:
        parameters['manifest_path'] = args.manifest
    started = time.monotonic()
    async with Client('http://127.0.0.1:8091/mcp', timeout=1850) as client:
        response = await client.call_tool(args.tool, parameters, raise_on_error=False)
    result = response.structured_content
    report = {'transport': 'SDK fallback, not native acceptance', 'elapsed_seconds': time.monotonic() - started, 'result': result}
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    raise SystemExit(0 if result and result.get('task_verified_success') is True else 1)


if __name__ == '__main__':
    asyncio.run(main())
