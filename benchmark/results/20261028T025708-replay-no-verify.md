## Eval results: replay (claude-opus-5-5)

Suite `benchmark\manifest.json`, run 2026-10-28T02:57:08+00:00.

| Mode | PRs (bug / clean) | Reported | Precision | Recall | Clean PRs flagged | Hypotheses | Verified | Cost / PR | Latency p50 / p90 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Witness (no-verify) | 5 / 5 | 18 | 67% (12/18) | 100% | 3 / 5 | 18 | 0 | $0.27 | 0s / 1s |

### Per case (no-verify)

| Case | Kind | Hypotheses | Reported | True positives | Fix passes hidden tests | Time |
| --- | --- | ---: | ---: | ---: | --- | ---: |
| humanize-bug-08cf2c30 | bug | 3 | 3 | 3 |  | 1s |
| marshmallow-bug-070dde08 | bug | 2 | 2 | 2 |  | 0s |
| more-itertools-bug-073d2342 | bug | 2 | 2 | 2 |  | 0s |
| pathe-bug-4bcf68fd | bug | 3 | 3 | 3 |  | 0s |
| sqlparse-bug-111b35cf | bug | 2 | 2 | 2 |  | 0s |
| humanize-clean-711df91a | clean | 1 | 1 | 0 |  | 0s |
| marshmallow-clean-07c9c18d | clean | 3 | 3 | 0 |  | 0s |
| more-itertools-clean-1f301094 | clean | 2 | 2 | 0 |  | 0s |
| sqlparse-clean-8238a9e4 | clean | 0 | 0 | 0 |  | 0s |
| std-env-clean-0766ba3f | clean | 0 | 0 | 0 |  | 0s |
