"""Client for the Editor-side authority. Never retries mutations."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'UnityReloadDiagnostics'))
from mcp_cli import Client
from observe_reconnect import tool_value

class GateClient:
    def __init__(self, timeout=30, instance=None):
        self.client = Client(timeout=timeout)
        self.token = None
        self.instance = instance

    def call(self, action, **parameters):
        if self.token and 'token' not in parameters:
            parameters['token'] = self.token
        arguments = {'tool_name': 'editor_queue', 'parameters': {'action': action, **parameters}}
        if self.instance:
            arguments['unity_instance'] = self.instance
        response = self.client.rpc('tools/call', {'name': 'execute_custom_tool', 'arguments': arguments})
        return tool_value(response)

    def request(self, owner, seconds=600):
        response = self.call('request', owner=owner, seconds=seconds)
        if response.get('success') is not True:
            raise RuntimeError(response)
        self.token = response['data']['token']
        return response

    def execute(self, command, parameters, operation=None):
        operation = operation or uuid.uuid4().hex
        result = self.call('execute', command=command, parameters=parameters, operation=operation)
        if result.get('success') is True:
            ack = self.call('ack', operation=operation)
            if ack.get('success') is not True:
                raise RuntimeError(ack)
        return result

    def close(self):
        self.client.close()

def staged_file(path, source=None, delete=False):
    """Prepare only. Calling this never writes in an import root."""
    root = Path(__file__).resolve().parents[2]
    destination = root / path
    item = {'path': path, 'expectedSha256': hashlib.sha256(destination.read_bytes()).hexdigest()
            if destination.is_file() else 'missing'}
    if delete:
        item['delete'] = True
    else:
        item['base64'] = base64.b64encode(Path(source).read_bytes()).decode('ascii')
    return item
