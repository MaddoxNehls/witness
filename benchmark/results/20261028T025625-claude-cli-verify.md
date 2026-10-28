## Eval results: claude-cli (claude-opus-5-5)

Suite `benchmark\manifest.json`, run 2026-10-28T02:56:25+00:00.

| Mode | PRs (bug / clean) | Reported | Precision | Recall | Clean PRs flagged | Hypotheses | Verified | Cost / PR | Latency p50 / p90 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Witness (verify) | 5 / 5 | 15 | 80% (12/15) | 100% | 2 / 5 | 18 | 15 | $1.32 | 43s / 77s |
| same run, all hypotheses (paired) | 5 / 5 | 18 | 67% (12/18) | 100% | 3 / 5 | 18 | 15 | $1.32 | 43s / 77s |

### Per case (verify)

| Case | Kind | Hypotheses | Reported | True positives | Fix passes hidden tests | Time |
| --- | --- | ---: | ---: | ---: | --- | ---: |
| humanize-bug-08cf2c30 | bug | 3 | 3 | 3 | yes, no, no | 62s |
| marshmallow-bug-070dde08 | bug | 2 | 2 | 2 | yes, yes | 43s |
| more-itertools-bug-073d2342 | bug | 2 | 2 | 2 | no, yes | 45s |
| pathe-bug-4bcf68fd | bug | 3 | 3 | 3 | yes, yes, yes | 48s |
| sqlparse-bug-111b35cf | bug | 2 | 2 | 2 | yes, yes | 42s |
| humanize-clean-711df91a | clean | 1 | 1 | 0 |  | 24s |
| marshmallow-clean-07c9c18d | clean | 3 | 2 | 0 |  | 77s |
| more-itertools-clean-1f301094 | clean | 2 | 0 | 0 |  | 34s |
| sqlparse-clean-8238a9e4 | clean | 0 | 0 | 0 |  | 7s |
| std-env-clean-0766ba3f | clean | 0 | 0 | 0 |  | 4s |
