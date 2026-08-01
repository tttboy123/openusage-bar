# Runtime Producer Kit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the first real, optional request producer for the frozen Runtime Observation plane without adding a network write endpoint, a LiteLLM dependency, or Loom scheduling authority.

**Architecture:** A standalone standard-library LiteLLM callback extracts only allowlisted terminal timing, public Provider/model identifiers, Token counters and optional estimated cost. It writes the existing strict `runtime-observation/v1` document to the packaged Collector over stdin; the Collector remains the only database writer. This slice freezes the producer boundary and LiteLLM compatibility fixture, while OTLP, CLIProxyAPI and local-agent hooks remain separately versioned follow-up adapters.

**Tech Stack:** Python 3.13 standard library, LiteLLM custom callback surface (optional runtime integration), existing Collector CLI and Runtime SQLite store, unittest, zsh packaging and macOS code signing.

---

## Frozen boundaries

- No Prompt, Response, message, tool argument/result, exception body, credential,
  raw LiteLLM payload, direct user/account identity or upstream request ID is
  serialized, logged or stored.
- The callback may inspect only the public model/provider selector, aggregate
  usage object, callback timestamps, opaque call/response ID for one-way local
  hashing, and optional `response_cost`.
- A missing usage object is **missing**, not zero. The callback drops that event
  rather than inventing zero Token usage. Failure-only observations therefore
  remain a later schema task unless the producer supplies real counters.
- `firstTokenAt` is nullable even when output Token usage is known. Missing TTFT
  is excluded from TTFT sample coverage; it is never replaced by end time.
- LiteLLM-calculated `response_cost` is `estimated`, never Provider-billed cost.
- The callback invokes only an absolute, regular, executable Collector path,
  uses `shell=False`, bounded stdin/output/time, and a minimal environment that
  excludes Provider credentials.
- The callback is best-effort telemetry: it never changes or fails the model
  request. Collector failure returns a local false result and emits no payload
  or secret-bearing diagnostic.
- No HTTP/OTLP listener, Local API write route, reservation, admission, routing,
  live Provider credential, external Canary, merge, push or release is part of
  WQ-22.

Primary compatibility evidence:

- [LiteLLM custom callbacks](https://docs.litellm.ai/docs/observability/custom_callback)
- [LiteLLM OpenTelemetry and content controls](https://docs.litellm.ai/docs/observability/opentelemetry_integration)
- [OpenTelemetry GenAI attributes](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)

### Task 1: Freeze the producer queue and compatibility fixture

**Files:**
- Modify: `docs/superpowers/plans/2026-07-18-openusage-work-queue.zh-CN.md`
- Create: `docs/superpowers/plans/2026-08-01-runtime-producer-kit.md`
- Create: `tests/fixtures/runtime-producers/litellm-success-v1.json`

- [x] **Step 1: Add WQ-22 through WQ-24 without changing WQ-16 evidence**

Record WQ-22 as the standalone LiteLLM callback, WQ-23 as a sanitized and
version-pinned OTLP/CLIProxyAPI adapter, and WQ-24 as Loom X1 read-only
observation. Keep external Canary at 0/5 and do not start its 30-day clock.

- [x] **Step 2: Freeze a content-bearing LiteLLM test input**

The fixture must include messages, a response body and a fake credential so the
test can prove they are never serialized. It must also contain:

```json
{
  "model": "openai/gpt-5",
  "custom_llm_provider": "openai",
  "litellm_call_id": "upstream-call-id-not-for-storage",
  "response_cost": "0.000025",
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 3,
    "total_tokens": 15,
    "prompt_tokens_details": {"cached_tokens": 4},
    "completion_tokens_details": {"reasoning_tokens": 1}
  }
}
```

- [x] **Step 3: Commit the plan slice**

```bash
git add docs/superpowers/plans/2026-07-18-openusage-work-queue.zh-CN.md \
  docs/superpowers/plans/2026-08-01-runtime-producer-kit.md \
  tests/fixtures/runtime-producers/litellm-success-v1.json
git commit -m "docs(runtime): queue the first local producer"
```

### Task 2: Preserve unknown TTFT instead of inventing a timestamp

**Files:**
- Modify: `openusage_bar/runtime_observation.py`
- Modify: `tests/test_runtime_observation.py`

- [x] **Step 1: Write the failing contract test**

Replace the old expectation that output Token usage requires `firstTokenAt`
with:

```python
def test_output_tokens_may_have_unknown_first_token_time(self):
    observation = valid_observation()
    observation["firstTokenAt"] = None
    decoded = decode_runtime_document(document([observation]))
    self.assertIsNone(decoded.observations[0].ttft_ms)
```

Also prove that a present first-Token timestamp must remain within the request
window and that reasoning Token count cannot exceed output Token count when the
convention says output includes reasoning.

- [x] **Step 2: Run RED**

```bash
.build-venv/bin/python -m unittest \
  tests.test_runtime_observation.RuntimeObservationContractTests.test_output_tokens_may_have_unknown_first_token_time -v
```

Expected: FAIL because the v1 decoder currently rejects null first-Token time
when output usage is nonzero.

- [x] **Step 3: Implement the minimal semantic correction**

Remove the output/non-null coupling. Keep timestamp ordering validation when the
field is present, and reject `reasoningTokens > outputTokens` for
`input_includes_cache` because reasoning is a subset of output under the
OpenTelemetry/LiteLLM convention.

- [x] **Step 4: Run GREEN and contract regression**

```bash
.build-venv/bin/python -m unittest tests.test_runtime_observation \
  tests.test_runtime_store tests.test_collector_cli.RuntimeObservationCLITests -v
```

- [x] **Step 5: Commit the contract correction**

```bash
git add openusage_bar/runtime_observation.py tests/test_runtime_observation.py
git commit -m "fix(runtime): preserve unknown first-token timing"
```

### Task 3: Build the standalone LiteLLM transformer

**Files:**
- Create: `integrations/litellm_openusage.py`
- Create: `tests/test_litellm_runtime_integration.py`
- Test: `tests/fixtures/runtime-producers/litellm-success-v1.json`

- [x] **Step 1: Write failing pure-transform tests**

Test `build_runtime_document(...)` with dictionary and attribute-style response
objects. Prove the result:

```python
{
    "schemaVersion": 1,
    "observations": [{
        "providerId": "openai",
        "modelId": "openai.gpt-5",
        "scopeRef": "anon_0123456789abcdef",
        "inputTokens": 12,
        "outputTokens": 3,
        "cacheReadTokens": 4,
        "cacheCreationTokens": 0,
        "reasoningTokens": 1,
        "totalTokens": 15,
        "tokenCountingConvention": "input_includes_cache",
        "sourceId": "litellm.callback.v1"
    }]
}
```

Decode the result with `decode_runtime_document()` rather than comparing only
implementation details. Assert that fixture Prompt, Response, fake key,
exception and raw call ID are absent from the encoded document. Assert missing
usage, unsafe IDs, invalid timestamps and inconsistent totals return `None`
without a synthetic zero row.

- [x] **Step 2: Run RED**

```bash
.build-venv/bin/python -m unittest tests.test_litellm_runtime_integration -v
```

Expected: FAIL because `integrations/litellm_openusage.py` does not exist.

- [x] **Step 3: Implement a standard-library-only transformer**

Create these public values:

```python
SOURCE_ID = "litellm.callback.v1"

def create_scope_ref() -> str: ...

def build_runtime_document(
    kwargs: object,
    response_obj: object,
    start_time: object,
    end_time: object,
    *,
    scope_ref: str,
    provider_map: dict[str, str],
    status: str,
) -> dict[str, object] | None: ...
```

Use exact safe getters rather than converting `kwargs` or `response_obj` to a
dictionary. Resolve Provider from `custom_llm_provider` or the model prefix only
through the explicit `provider_map`. Normalize model `/` to `.`, validate all
public IDs, hash the raw call/response identifier with SHA-256 and retain only
the first 32 lowercase hexadecimal characters after `obs_`. Convert dollars to
integer micro-units with `Decimal(str(value))`; mark rows with calculated cost
as `estimated`, otherwise `provider_reported`.

- [x] **Step 4: Run GREEN**

```bash
.build-venv/bin/python -m unittest tests.test_litellm_runtime_integration -v
```

- [x] **Step 5: Commit the transformer**

```bash
git add integrations/litellm_openusage.py \
  tests/test_litellm_runtime_integration.py
git commit -m "feat(runtime): normalize LiteLLM terminal usage"
```

### Task 4: Add bounded sync and async callback delivery

**Files:**
- Modify: `integrations/litellm_openusage.py`
- Modify: `tests/test_litellm_runtime_integration.py`

- [ ] **Step 1: Write failing delivery tests**

Instantiate `OpenUsageRuntimeLogger` with an injected runner. Prove:

- success delivery invokes `[collector, "runtime-ingest", "--database", path]`
  with `shell=False`, a three-second timeout, compact JSON on stdin and bounded
  captured output;
- the child environment contains only the explicit safe allowlist and never an
  inherited `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, cookie or session;
- invalid/missing usage launches no child and records no zero;
- nonzero, timeout and exception results are swallowed and return false;
- sync and async success methods deliver the same canonical document;
- failure methods deliver only when real Token counters exist and never include
  an exception body.

- [ ] **Step 2: Run RED**

```bash
.build-venv/bin/python -m unittest tests.test_litellm_runtime_integration -v
```

Expected: FAIL because the callback logger does not exist.

- [ ] **Step 3: Implement bounded delivery**

Provide an optional LiteLLM `CustomLogger` base when LiteLLM is importable and a
dependency-free fallback base for repository tests. Implement:

```python
class OpenUsageRuntimeLogger(CustomLogger):
    def log_success_event(self, kwargs, response_obj, start_time, end_time): ...
    def log_failure_event(self, kwargs, response_obj, start_time, end_time): ...
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time): ...
    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time): ...
```

Delivery must be best-effort and must not print callback input, Collector output
or exception text. Async delivery uses `asyncio.to_thread` so it does not block
the event loop.

- [ ] **Step 4: Run GREEN and privacy regression**

```bash
.build-venv/bin/python -m unittest tests.test_litellm_runtime_integration \
  tests.test_privacy_scan -v
```

- [ ] **Step 5: Commit delivery**

```bash
git add integrations/litellm_openusage.py \
  tests/test_litellm_runtime_integration.py
git commit -m "feat(runtime): deliver LiteLLM observations locally"
```

### Task 5: Package, document and smoke the real producer path

**Files:**
- Create: `docs/runtime-producers.md`
- Create: `scripts/runtime_producer_smoke.py`
- Modify: `scripts/build_app.sh`
- Modify: `tests/test_build_script.py`
- Modify: `scripts/privacy_scan.py` only if its public allowlist needs the new
  safe source identifier.

- [ ] **Step 1: Write failing packaging tests**

Require `build_app.sh` to copy the standalone module to:

```text
OpenUsage Bar.app/Contents/Resources/Integrations/litellm_openusage.py
```

Require the build to run `runtime_producer_smoke.py` against the packaged module
and packaged Collector. The smoke must create a temporary runtime database,
invoke one callback with content-bearing fake input, query `runtime-summary`,
verify one 15-Token row, mode `0600`, no private strings and print:

```text
runtime_producer_smoke_ok producer=litellm.callback.v1 observations=1
```

- [ ] **Step 2: Run RED**

```bash
.build-venv/bin/python -m unittest \
  tests.test_build_script.BuildScriptContractTests.test_build_packages_and_smokes_litellm_runtime_producer -v
```

Expected: FAIL because the packaged integration and smoke do not exist.

- [ ] **Step 3: Implement package copy and smoke**

Use only absolute paths, regular-file/symlink checks, temporary storage and
bounded subprocesses. Add the packaged integration file to the privacy scan.

- [ ] **Step 4: Write the quick-start guide**

Document LiteLLM SDK and Proxy registration, one-time `create_scope_ref()` use,
explicit Provider mapping, the required app path, how to query
`runtime-summary`, data retained/omitted, missing-usage behavior, and uninstall
cleanup. State that OpenTelemetry must use `NO_CONTENT`, but direct OTLP ingest
is not shipped in WQ-22 because LiteLLM also emits identity metadata unless a
strict allowlist processor removes it.

- [ ] **Step 5: Run package tests and commit**

```bash
.build-venv/bin/python -m unittest tests.test_build_script \
  tests.test_litellm_runtime_integration -v
git add docs/runtime-producers.md scripts/build_app.sh \
  scripts/runtime_producer_smoke.py tests/test_build_script.py
git commit -m "feat(runtime): package the LiteLLM producer kit"
```

### Task 6: Complete WQ-22 repository verification

**Files:**
- Modify: `docs/superpowers/plans/2026-07-18-openusage-work-queue.zh-CN.md`
- Modify: `docs/superpowers/plans/2026-08-01-runtime-producer-kit.md`

- [ ] **Step 1: Prove authority and data-plane separation**

```bash
! rg -n "RuntimeStore|runtime_observation|runtime\.sqlite3" \
  openusage_bar/activity_store.py openusage_bar/local_api.py \
  openusage_bar/activity_schema.py
! rg -n "reservation|admission|routingPolicy|prompt|response" \
  integrations/litellm_openusage.py
```

The second command may match comments or safe test names only after manual
review; it must not match a serialized field or diagnostic.

- [ ] **Step 2: Run focused and complete release gates**

```bash
.build-venv/bin/python -m unittest tests.test_runtime_observation \
  tests.test_runtime_store tests.test_litellm_runtime_integration \
  tests.test_collector_cli.RuntimeObservationCLITests -v
scripts/audit_dependencies.sh
.build-venv/bin/python scripts/release_secret_scan.py --history
scripts/build_app.sh
```

- [ ] **Step 3: Record exact evidence without widening claims**

Record test totals, Python and Swift coverage, dependency/secret/privacy scan,
signed bundle and packaged producer smoke. Keep real LiteLLM account traffic,
OTLP/CLIProxyAPI adapters, Loom X1, external Canary, merge, push and publication
open.

- [ ] **Step 4: Commit verification**

```bash
git add docs/superpowers/plans/2026-07-18-openusage-work-queue.zh-CN.md \
  docs/superpowers/plans/2026-08-01-runtime-producer-kit.md
git commit -m "docs(runtime): record producer-kit verification"
```

## Completion boundary

WQ-22 is repository-complete only when a content-bearing LiteLLM callback input
is transformed into one canonical observation by the packaged integration and
packaged Collector, with strict privacy, Unknown-not-zero and full release gates
proven. It does not claim real-account traffic, OTLP/CLIProxyAPI compatibility,
Loom scheduling readiness, an external Canary machine, merge or publication.
