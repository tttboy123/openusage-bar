# Gateway Performance Evidence

This document records the current Gateway performance evidence contract. It is
about reproducible evidence shape and release gates, not about claiming final
performance numbers for the current development round.

## Evidence Status

The current evidence fixture is
`tests/fixtures/gateway/performance-v1.json`. The report schema is
`docs/schemas/gateway-performance-v1.schema.json`, and the local runner is
`scripts/measure_gateway_performance.py`.

No final release performance report is committed in this round. Every v1
report now carries the exact top-level field `"evidenceClass":"diagnostic"`.
Schema verification proves only the closed report shape and internal
accounting; neither `verify` nor `run --enforce` promotes a report to release
evidence. A later release-evidence format must add independently controlled
reference-machine admission instead of trusting a caller-supplied label.

## Release Gates

| Scenario | Release gate | Current result |
| --- | --- | --- |
| Should-Send authenticated loopback | p99 < 50 ms | Pre-optimization clean local diagnostic: 16.247 ms worst p99, pass. |
| Responses proxy overhead, paired | signed p99 delta < 200 ms | Pre-optimization clean local diagnostic: 86.057 ms worst signed p99, pass. |
| Exact cache core lookup | p99 < 5 ms | Pre-optimization clean local diagnostic: 18.899 ms worst p99, fail. |
| Exact cache authenticated loopback | informational only | Pre-optimization clean local diagnostic: 148.199 ms worst p99; informational only. |
| Responses throughput | every complete one-second bucket >= 100 successes | Pre-optimization clean local diagnostic: 9/s worst complete bucket with client timeouts, fail. |

The cache scenario status is derived from `responsesExactCacheHit.coreLookup`.
`responsesExactCacheHit.e2eAuthenticatedLoopback` is nested with
`informationalOnly: true`; it exists to diagnose end-to-end HTTP/server overhead
and must not be used as the cache release gate.

## Frozen Workload

The v1 fixture is deliberately exact rather than machine-scaled:

| Surface | Frozen input |
| --- | --- |
| Activity facts | 1,000 quota-state rows |
| Gateway telemetry | 4,000 aggregate rows: 1,000 in-window matches, 1,000 out-of-window matches, 1,000 other-model rows, and 1,000 other-provider rows |
| Cache | 1,000 entries; entry 500 is the hot exact hit; zero prefix hashes per entry |
| Should-Send request | 82-byte canonical JSON body |
| Responses request | 4,096-byte Gateway envelope with a 4,041-byte canonical Provider request |
| Synthetic response | 4,096-byte output text in fixed 4,307-byte SSE framing |

Every latency scenario uses 100 warmups followed by 2,000 measured attempts in
each of three rounds. Throughput uses 16 clients, a 32-thread server cap, 30
one-second buckets, connection-close HTTP, and a five-second client timeout.
The fixture-only admission limiter is 10,000 capacity with 10,000/second refill;
it is a benchmark control and does not change the product default limiter.

The cache workload primes the streaming response once and requires `miss`, then
repeats the identical request and requires `exact_hit`, `attemptCount: 0`, and no
additional fixture egress call. This proves that the measured core lookup and
authenticated loopback refer to the same exact-hit contract.

## Measurement Semantics

Latency summaries use nearest-rank p99 with `ceil(percentile * sample_count /
100)`. Each scenario runs three rounds; the published summary selects the worst
valid round. A round is invalid if warmups, sample counts, order, or clock
monotonicity violate the frozen fixture contract.

Proxy overhead is paired and signed: each sample records direct fixture egress
and Gateway loopback in alternating order, then computes `gateway_ns -
direct_ns`. Negative deltas are retained and counted; they are not clamped to
zero.

Throughput uses terminal completion timestamps. Only completions in the
half-open measurement window `[window_start, window_end)` count toward the
per-second buckets. Completions at or after `window_end` are late completions
and are reported separately in `lateCompletionCount`; they are excluded from
the buckets and are not reclassified as request errors. Real request errors,
timeouts, or HTTP 429 responses fail the round. Completions before
`window_start` are clock regressions and invalidate the contract.

## Reference Machine Rules

A future report may be considered a release candidate only when all of the
following are independently established:

- The source tree state in the report is `clean`.
- The run happens on the intended reference class of machine, on AC power where
  applicable, with no intentional competing workload.
- The report is produced by `scripts/measure_gateway_performance.py run`, then
  verified by `scripts/measure_gateway_performance.py verify`.
- The report contains only the allowlisted aggregate fields from the schema.
- The report is attached to manual CI evidence or release notes as non-secret
  JSON; local databases, prompts, responses, credentials, process IDs, and local
  paths are never attached.

The current v1 format intentionally remains `diagnostic` even when all of
those prerequisites appear true. This prevents a local flag or shared hosted
runner from self-authorizing release evidence.

The runner resolves the real Git `HEAD` and full tracked, staged, untracked,
and submodule worktree state. A mismatched `--source-commit` or explicit
`--source-tree-state` fails before measurement. The script also records bounded
machine facts such as OS, architecture, CPU model, memory, storage class,
filesystem, power state, and Python version. Those fields help interpret the
run; they do not by themselves prove that the machine was idle.

## Manual CI

The desktop build workflow has a dispatch-only `gateway-performance` job. It
runs the unit contract and smoke checks, invokes the performance runner,
verifies the report, and uploads `gateway-performance-v1.json`. Absolute budgets
remain non-blocking because the shared runner job deliberately omits
`--enforce`; the artifact is diagnostic CI evidence, not a reference-machine
release claim. Its closed `evidenceClass` makes that boundary machine-readable.

## Commands

Run the credential/network isolation smoke first:

```sh
python scripts/measure_gateway_performance.py smoke
```

Create and verify a report bound to the current repository revision:

```sh
python scripts/measure_gateway_performance.py run \
  --source-commit "$(git rev-parse HEAD)" \
  --source-tree-state auto \
  --output <gateway-performance-v1.json>
python scripts/measure_gateway_performance.py verify \
  --report <gateway-performance-v1.json>
```

Use `--enforce` only when the diagnostic command should exit nonzero for failed
thresholds. It never changes `evidenceClass`, and must not be used to describe a
shared CI runner or foreground-loaded developer machine as a release gate.

## Privacy and Unit Compatibility

The performance fixture uses synthetic Provider inputs, synthetic SSE output,
synthetic capacity facts, synthetic Gateway telemetry, and a local fixture
transport. The authenticated loopback smoke is expected to make one fixture
egress call and zero real credential reads or real Provider network calls.

Should-Send prediction may use burn rate only when the remaining quota and the
observed burn rate are in compatible units, such as remaining Tokens divided by
Tokens per minute. A remaining ratio is not a Token balance and must not be
divided by a Token burn rate to fabricate `predicted_exhaustion_minutes`.

## Current Non-Release Diagnostics

These diagnostics are useful for development confidence but are not final
performance evidence:

| Diagnostic | Status |
| --- | --- |
| `python -m unittest tests.test_gateway_performance_measurement tests.test_gateway_should_send tests.test_gateway_cache` | Local diagnostic PASS observed during documentation update. |
| `python scripts/measure_gateway_performance.py smoke` | Local diagnostic PASS observed during documentation update: fixture egress only, no real credentials or Provider network. |
| Clean local three-round report at `b8c917c`, 2026-08-10 | Pre-optimization diagnostic, Apple M3 on battery with load approximately 4.7/8 cores: Should-Send worst p99 16.247 ms; paired proxy worst signed p99 86.057 ms; cache core worst p99 18.899 ms; informational authenticated cache loopback 148.199 ms; throughput worst complete bucket 9 successes/s with 16 total client timeouts across three rounds. Overall fail; not release evidence. |
| Evidence-led listener change | The 16-client workload used a 32-thread cap while the inherited TCP listen backlog was only 5. The listener backlog now follows the already validated `max_threads`; authentication, worker capacity, deadlines, connection-close behavior, and telemetry semantics are unchanged. Cache security checks remain unchanged because isolated hot lookup p99 was approximately 0.065 ms and did not justify weakening per-lookup file identity or permission validation. |
| Dirty local three-round report, 2026-08-09 | Schema-valid diagnostic with overall `fail`: Should-Send worst p99 66.165 ms; paired proxy worst p99 delta 199.710 ms; cache core worst p99 6.250 ms; informational authenticated cache loopback 155.764 ms; throughput worst complete bucket 13 successes/s with real client timeouts. The source tree was dirty and the machine was under severe foreground load, so this is not release evidence. |
