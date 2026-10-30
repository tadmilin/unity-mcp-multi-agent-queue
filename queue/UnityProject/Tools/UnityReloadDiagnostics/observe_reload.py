"""Read only the new log bytes of an authorized workspace reload experiment."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import time


def extract_profile(text):
    text = text.replace("\r\n", "\n")
    match = re.search(r"Domain Reload Profiling: (\d+)ms\n((?:[ \t]+[^\n]*\n)+)(?:\n|(?=\S))", text)
    if not match:
        return None
    phases = []
    for line in match.group(2).splitlines():
        phase = re.match(r"([ \t]+)(.+?) \((\d+)ms\)$", line)
        if phase:
            phases.append({"indent": len(phase[1]), "name": phase[2], "inclusive_ms": int(phase[3])})
    return {"domain_reload_ms": int(match[1]), "phases": phases, "raw_block": match[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=360)
    args = parser.parse_args()
    if args.timeout <= 0 or args.timeout > 600:
        parser.error("timeout must be in (0, 600]")
    if args.output.exists():
        parser.error("Output exists; never overwrite an experiment")
    before = json.loads(args.before.read_text(encoding="utf-8-sig"))
    log = Path(before["logPath"]).resolve()
    log.relative_to(Path.cwd().resolve())  # Refuse logs outside the authorized workspace.
    start = time.monotonic()
    data = bytearray()
    profile = None
    status = "timeout"
    print("Observing authorized workspace log from saved byte offset", flush=True)
    with log.open("rb") as stream:
        if log.stat().st_size < before["logOffset"]:
            raise RuntimeError("Log was truncated; baseline offset is no longer valid")
        stream.seek(before["logOffset"])
        while time.monotonic() - start < args.timeout:
            chunk = stream.read(65536)
            if chunk:
                data.extend(chunk)
                profile = extract_profile(data.decode("utf-8", errors="replace"))
                if profile:
                    status = "profile_found"
                    break
                if len(data) >= 8 * 1024 * 1024:
                    status = "capture_limit"
                    break
            else:
                time.sleep(0.5)
    result = {"status": status, "observed_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "before": before, "bytes_read": len(data), "profile": profile,
              "note": "Native log phase timings; not summed. Observer completion does not prove MCP reconnection."}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"status": status, "domain_reload_ms": profile["domain_reload_ms"] if profile else None}), flush=True)
    return 0 if profile else 2


if __name__ == "__main__":
    raise SystemExit(main())
