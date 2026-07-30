# Performance and lightweight-host budgets

OpenUsage Bar uses a native Swift menu-bar host and Activity window, while the
Python collector remains the sole ledger writer. Performance decisions are
based on repeated live measurements, not a preference for one implementation
language.

## Current decision

No resident-host migration is justified by the installed 0.6.0 candidate
baseline. The resident
status host and collector remain inside the initial CPU, physical-footprint,
wakeup, package-size, and refresh-duration budgets. Activity is measured only
while its window is open; its absence is `Unknown`, never numeric zero.

Python remains the sole ledger writer. A Swift read-only Agent plus a one-shot
Python collector may be evaluated only after the migration gate below is met.

## Reproduce the baseline

Open Usage Details before measuring Activity, then run:

```bash
python3 scripts/measure_performance.py \
  --rounds 3 \
  --idle-seconds 10 \
  --refresh-timeout 95 \
  --output /tmp/openusage-performance.json
python3 scripts/privacy_scan.py /tmp/openusage-performance.json
```

The tool resolves the installed bundle, verifies its bundle identifier and
exact process executables, and reads `proc_pid_rusage` counters before and
after each interval. It reports physical footprint rather than virtual memory,
CPU-time deltas rather than a single `ps` sample, and package/interrupt wakeup
deltas rather than cumulative lifetime totals.

Every report contains at least three rounds. It omits PIDs, commands,
executable paths, Provider identities, endpoints, credentials, payloads,
prompts, responses, and account identity. Output files are mode `0600`.
Missing, restarted, or unverified processes remain unavailable.

## Initial budgets

| Metric | Initial maximum | 0.4.4 build 8 | 0.6.0 build 9 |
| --- | ---: | ---: | ---: |
| Logical app size | 75 MiB | 47.7 MiB | 48.1 MiB |
| Resident CPU p95 | 1.0% of one core | 0.001% | 0.001% |
| Resident wakeups p95 | 5.0/s | 2.298/s | 2.200/s |
| Resident physical footprint peak | 100 MiB | 77.5 MiB | 50.4 MiB |
| Activity physical footprint peak | 120 MiB | 95.1 MiB | 97.8 MiB |
| Full refresh duration p95 | 90 s | 41.490 s | 52.044 s |

The committed evidence is
[`performance-baselines/0.4.4-build8-2026-07-29.json`](performance-baselines/0.4.4-build8-2026-07-29.json).
It contains three 10-second idle rounds and three successful full refreshes.
The report passed the repository privacy scanner with zero findings.

The installed 0.6 candidate evidence is
[`performance-baselines/0.6.0-build9-2026-07-30.json`](performance-baselines/0.6.0-build9-2026-07-30.json).
It also contains three 10-second idle rounds and three successful full
refreshes. All six budgets pass; refresh has zero timeout and failure results,
and the report passes the repository privacy scanner with zero findings.

These are regression budgets, not universal performance claims. A candidate
must be compared with the same version, Provider configuration class, window
state, and measurement plan. Thermal state and other foreground workloads
should be stable and noted outside the public report without adding device or
account identity.

## Source-class refresh timing

The 0.4.4 report deliberately declares
`perSourceTiming: "not_observable"`. It measures the full configured refresh,
but the current public and diagnostic contracts do not expose each Provider's
network, local-file, or child-process duration. The report therefore cannot
attribute the 41.490-second p95 to a Provider, timeout, or backoff path.

The 0.6 candidate adds opt-in instrumentation used only by the performance
measurement tool. Each refresh round aggregates monotonic duration and
success, timeout, backoff, unavailable, or failed outcome into one of three
classes: `network`, `local_file`, or `child_process`. It intentionally does not
retain a Provider ID, source ID, account reference, endpoint, local path,
request/response body, credential, prompt, or response. The report can locate
the expensive execution class without making account configuration
fingerprintable.

The installed 0.6 candidate recorded maximum per-operation durations of
10.048 seconds for `network`, 0.150 seconds for `local_file`, and 38.133
seconds for `child_process`. Across three refreshes, all class samples had zero
timeout and failed outcomes. The longer end-to-end refresh is therefore
bounded and dominated by child-process collection; it does not justify a
resident-host language migration.

These metrics are not published through Local API v1 or the diagnostics
bundle; either exposure requires a separate compatibility and privacy review.

Provider-specific timing remains intentionally unobservable. A Provider-level
optimization still requires separate, privacy-reviewed evidence rather than
inferring identity from source-class aggregates.

## Migration gate

A resident-host migration proposal is admissible only when:

1. a budget fails in two same-version baselines after separate boots;
2. each baseline contains at least three idle and three refresh rounds;
3. Provider/source timing is observable enough to locate the cost;
4. the proposal states the measured baseline, target benefit, compatibility
   cost, ledger/API impact, staged rollout, and rollback;
5. a separate ADR approves any change to ledger ownership.

Until all five conditions hold, optimize the measured hot path in place. Do
not rewrite mature Provider or ledger logic merely to change languages.
