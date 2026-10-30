"""Local-only, sequential MCP client for CLI diagnostics; no agent or paid API."""
import argparse
import json
from pathlib import Path
import urllib.request


class Client:
    def __init__(self, timeout=20):
        self.url = "http://127.0.0.1:8080/mcp"
        self.timeout = timeout
        self.session = None
        self.sequence = 0
        self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "reload-diagnostics-cli", "version": "1"}})
        self.rpc("notifications/initialized", {}, notification=True)

    def rpc(self, method, params, notification=False):
        self.sequence += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = self.sequence
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.url, json.dumps(payload).encode(), headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            self.session = response.headers.get("Mcp-Session-Id", self.session)
            raw = response.read().decode()
        if not raw.strip():
            return None
        if raw.lstrip().startswith("{"):
            return json.loads(raw)
        for line in raw.splitlines():
            if line.startswith("data:"):
                item = json.loads(line[5:])
                if item.get("id") == payload.get("id"):
                    return item
        raise RuntimeError("No matching MCP response")

    def close(self):
        if self.session:
            request = urllib.request.Request(self.url, headers={"Mcp-Session-Id": self.session}, method="DELETE")
            try:
                with urllib.request.urlopen(request, timeout=5):
                    pass
            except Exception:
                pass
            self.session = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--resource")
    group.add_argument("--code-file", type=Path)
    group.add_argument("--list-tools", action="store_true")
    group.add_argument("--tool-file", type=Path, help="JSON containing name and arguments")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite evidence")
    client = Client(args.timeout)
    try:
        if args.resource:
            result = client.rpc("resources/read", {"uri": args.resource})
        elif args.list_tools:
            result = client.rpc("tools/list", {})
        elif args.tool_file:
            result = client.rpc("tools/call", json.loads(args.tool_file.read_text(encoding="utf-8-sig")))
        else:
            result = client.rpc("tools/call", {"name": "execute_code", "arguments": {
                "action": "execute", "code": args.code_file.read_text(encoding="utf-8-sig")}})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=True)[:14000])
    finally:
        client.close()


if __name__ == "__main__":
    main()
