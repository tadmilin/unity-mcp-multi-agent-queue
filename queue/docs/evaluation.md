# Historical evaluation, 30 October 2026

The comparison used three stock-MCP sessions and three queue sessions, all with the same recorded model/effort. The original six transcripts were hash-verified locally. Raw transcripts and application assets are not published here.

The stock arm continued repaired work and added VFX preparation; the queue arm started from different inputs and used a reduced rough-rig scope. The observed envelopes were **29m19.109s** and **57m05.427s** respectively. These are not equal-workload completion times.

| Observation | Stock | Queue |
| --- | ---: | ---: |
| Workers with a completed own Play phase within 29m19.109s | 1/3 | 1/3 |
| Workers reaching own Play over each arm's full observed run | 1/3 | 3/3 |
| Endings citing shared Unity context with Unity work pending | 2 | 0 |
| Recorded shell invocation sites | 79 | 255 |

The ending observation includes other stock connection/content issues; shared-context contention is not established as the sole cause. Queue workers needed compile repair and diagnostic status checks. Reaching Play is not full visual/content acceptance.

## Calls: separate the layers

- The queue arm recorded **95 local `observeWorkflow` invocations**: 40 for one worker and 55 for another. These observe the existing Promise. They are not 95 measured RPCs saved compared with stock.
- The worker with 55 observations retained complete transport traces for **9 jobs, each submitted once**. Those traces include 9 initialize messages, 9 initialized notifications, 9 capability calls, 9 submissions, and 9 HTTP DELETE session closes.
- The central executor ledger for **25 jobs** records **467 paired gate intents/responses plus 110 additional status calls**: at least 577 supported gate-action calls in that layer. Import-confirmation statuses are already included in 110. One extra owner-recovery release receipt is accounted for separately.
- The 467 actions comprise request 25, claim 25, execute 149, acknowledge 159, renew 65, release 24, write-files 10 and import 10. They are not all retries. Claim was attempted once for each of these 25 jobs. No identical execute payload was repeated within one job in that ledger sample.
- Manual gate operations outside the executor still encountered a held claim and invalid-token refusals, followed by successful retries. Two rejected manifests were corrected before admission. An external connector retry still returned HTTP 429.
- The stock arm has 71 direct Unity-tool and 21 Unity-resource invocation records. This agent-layer total cannot be compared directly with the queue's executor-layer 577.
- Stock's 11 exact-repeat groups contain 15 occurrences after the first: one identity retry, two state reads after an unavailable response, and twelve other observations. This is not a total semantic-retry count.

## Supported conclusion

The observed workflow can retain a pending request and let a worker continue after waiting, without resubmitting the workload on each observation. The recorded comparison does **not** establish lower total calls, lower total cost, faster completion, zero interference, assistance-free recovery, or superior content quality.

The next quantitative study should match initial inputs, task scope, stopping rules and assistance policy, repeat both arms, and reconcile the same call layers. Runtime integration of this source export is a separate validation task.
