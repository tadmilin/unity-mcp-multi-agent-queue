# Recovering a result without repeating Editor work

A disconnected client does not prove that Unity missed the command. First query the existing job with the same mode and submission key:

```json
{
  "tool_name": "editor_queue",
  "parameters": {
    "action": "workflow_result",
    "mode": "phase",
    "submission_key": "your-original-key"
  }
}
```

Do not create a replacement key to recover an uncertain result. The service keeps the job and its receipts independently of the observing client.

## Supported cases and conditions

| Situation | Recovery behavior | Required evidence |
| --- | --- | --- |
| Execute response is lost | Read the original operation receipt; recover its completed result. Never execute the command again. | Same authenticated owner/ticket, exact operation, known terminal result and available observation budget. |
| Import confirmation returns unavailable or non-success | Wait for a new native connection registration, then repeat only the read-only confirmation. | Same native process epoch, original operation/receipt/owner/lease, expected Editor PID, completed import and clean idle Editor; script imports also require a changed domain boot. The original import deadline is retained. |
| Acknowledgement (ACK) response is lost | Read the receipt. If already acknowledged, continue. If still complete, send at most one supplemental ACK, then read back the acknowledged state. | Same token/ticket/owner, unblocked and unexpired lease, original Editor PID, exact operation/result and stable nonempty receipt hash. One fixed recovery deadline bounds the status and supplemental ACK calls. |
| Evidence disagrees or a deadline expires | Stop automatic progression and quarantine the job. | A later operator resolution needs evidence that the Editor and gate are fully released and clean. It records an abandoned job, not task success. |

ACK records receipt acceptance; it does not run the command again. The authority permits only the transition from `complete` to `acknowledged`. A late original ACK can win that transition; the supplemental request then fails harmlessly, and the executor still requires a matching acknowledged status. There is no third ACK and no repeated Play, Stop, file write or import. A restarted cleanup worker without the original Editor identity cannot use this supplemental path.

Separate narrow cleanup routes handle a proven delayed owned Stop, opted-in import cleanup, and an external Play interruption after completed Edit-only work. They retain their existing evidence checks. Cleanup recovery does not change an abandoned task into a successful task.

## Time: observed intervals are not guarantees

- A gate RPC defaults to 30 seconds. This is one request's timeout, not total recovery time.
- `queue_timeout_seconds` defaults to 900 seconds (allowed 10–1700). ACK reconciliation uses that budget from reconciliation entry; reconnects do not restart it. This is not an end-to-end job deadline.
- Quarantine scans are scheduled five seconds after the preceding scan. Jobs and recovery share one executor, so this does not promise recovery within five seconds.
- Eight historical import reconfirmations took 1.575–2.837 seconds from an unavailable confirmation to a confirmed result; median 2.654 seconds. These are historical v3 observations, not whole compilation times.
- One new isolated Unity qualification naturally lost the start-Play ACK. After the fix, the executor recovered in **10.371 seconds** from the failed ACK response to confirmed acknowledgement, issued one supplemental ACK, completed the task and released the gate. The ledger contains exactly one start-Play and one stop-Play execution. This is one observed case, not an SLA or a stock comparison.

## Validation scope

The focused phase/import/recovery suite passed 109 tests. The isolated Unity 6000.6.5f1 project passed a basic check, a one-second owned Preview with natural ACK recovery, and one real C# write/import/domain reload with a loaded-constant completion check. The import used one write and one import; it did not encounter unavailable confirmation, so it provides no new import-reconfirmation timing.

The initial Preview failed and required audited operator cleanup, recorded as abandoned. Its evidence remains. The next accepted job waited behind that quarantine and its first client observation timed out; its successful terminal result was retrieved using the same key. Its total client/queue time must not be presented as recovery latency. Once that job began, it completed without operator intervention.

See [recovery matrix](../experiments/recovery-audit/recovery-matrix.md), [historical brackets](../experiments/recovery-audit/observed-brackets.json) and the sanitized [live qualification](recovery-live-validation.json). The full stock/queue contention and model-token comparison remains unfinished.
