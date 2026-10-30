import test from 'node:test';
import assert from 'node:assert/strict';
import { startWorkflow, observeWorkflow, WORKSPACE } from './direct_client.mjs';

test('parallel handles retain separate wire traces; local waits send no RPC', async () => {
  const releases = [];
  const sent = [];
  const fetchImpl = async (_, options) => {
    if (options.method === 'DELETE') return new Response(null, { status: 204 });
    const request = JSON.parse(options.body);
    sent.push(request);
    if (request.method === 'notifications/initialized') return new Response(null, { status: 202 });
    let result = {};
    if (request.params?.name === 'unity_workflow_capabilities') result = { structuredContent: { workspace: WORKSPACE } };
    if (request.params?.name === 'unity_job_run') {
      result = { structuredContent: await new Promise(resolve => releases.push(resolve)) };
    }
    return new Response(JSON.stringify({ jsonrpc: '2.0', id: request.id, result }),
      { headers: { 'mcp-session-id': 'SECRET-session-never-export' } });
  };
  const handles = ['parallel-a', 'parallel-b'].map(key => startWorkflow({key, manifest: 'staged/check.json', fetchImpl}));
  try {
    await Promise.all(handles.map(handle => observeWorkflow(handle, {waitMilliseconds: 20})));
    assert.equal(releases.length, 2, 'both submissions remain outstanding concurrently');
    const count = sent.length;
    await Promise.all(handles.map(handle => observeWorkflow(handle, {waitMilliseconds: 1})));
    assert.equal(sent.length, count, 'collection must not poll or resubmit');
    for (const handle of handles) {
      assert.ok(Array.isArray(handle.trace), 'missing per-key wire trace');
      assert.deepEqual(handle.trace.filter(e => e.stage === 'send').map(e => e.tool || e.method),
        ['initialize', 'notifications/initialized', 'unity_workflow_capabilities', 'unity_job_run']);
      assert.ok(handle.trace.every(e => e.key === handle.key));
      assert.equal(JSON.stringify(handle.trace).includes('SECRET'), false);
      const copy = handle.trace;
      copy.length = 0;
      assert.ok(handle.trace.length > 0, 'caller cannot erase retained evidence');
    }
  } finally {
    releases.forEach((resolve, index) => resolve({job_id: String(index), verified_success: index === 0, task_verified_success: index === 0}));
    await Promise.all(handles.map(handle => handle.completion));
  }
  for (const handle of handles) {
    assert.equal(handle.trace.filter(e => e.stage === 'send' && e.method === 'DELETE').length, 1);
    assert.equal(handle.trace.filter(e => e.stage === 'outcome').length, 1);
    assert.equal(handle.trace.find(e => e.stage === 'outcome').job_id, handle.result.job_id);
    assert.equal(handle.trace.find(e => e.stage === 'outcome').task_verified_success, handle.result.task_verified_success);
  }
});
