# Loom OpenUsage Resource Observer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Loom consume OpenUsage Bar resource facts as bounded, attributable, observe-only evidence without transferring scheduling or state authority to OpenUsage Bar.

**Architecture:** A Loom-side Unix-socket client reads only the public Local API and normalizes capacity facts into a versioned `ResourceObservation`. V0 does not call Scheduler or ProviderRouter. Once SessionBindingController is present on the selected Loom base, a separate event records the observation digest without changing Session state.

**Tech Stack:** Python 3.11+ standard library, AF_UNIX, HTTP/1.1, frozen dataclasses, canonical JSON/SHA-256, pytest, Loom Event Journal.

---

**Codex skill resolution:** In this environment, `superpowers:executing-plans` maps to the installed `executing-plans` skill. Use the subagent-driven option only when `subagent-driven-development` is actually available.

## Repository and authority preflight

The inspected Loom worktree at `/Users/lune/Documents/Codex/2026-06-18/hermes-openclaw/agent-platform` contains unrelated user changes. Implementation must use a new worktree and must read that repository's `AGENTS.md` first.

The current Loom main worktree does not contain `SessionBindingController`; that implementation exists in the isolated `agent-platform-rd-source` repository. Tasks 1-3 can land observe-only on current Loom. Task 4 must use a base where the reviewed SessionBinding implementation has entered the intended mainline.

### Task 1: Define the normalized resource observation

**Files in Loom repository:**
- Create: `devkit/resource_observation.py`
- Create: `tests/test_resource_observation_2026_07_18.py`

- [ ] **Step 1: Write failing validation and digest tests**

Cover direct/derived/unknown quality, stale facts, missing numeric values, invalid ratios, direct identity, deterministic ordering, and secret-like unknown fields.

- [ ] **Step 2: Define the contract**

```python
from dataclasses import dataclass
from typing import Literal

ObservationState = Literal["known", "estimated", "unknown"]
ObservationFreshness = Literal["live", "stale", "unavailable"]

@dataclass(frozen=True)
class ResourceObservation:
    api_version: Literal["loom.resource.observation/v1"]
    source: Literal["openusage-bar"]
    source_revision: int
    generated_at: str
    provider_id: str
    account_ref: str | None
    quota_name: str
    state: ObservationState
    interval: tuple[float, float] | None
    resets_at: str | None
    observed_at: str
    freshness: ObservationFreshness
    source_id: str
    quality: str
```

Rules:

- `known` requires `quality` of `direct` or `authoritative` and a bounded interval.
- `estimated` requires `quality` of `derived` and a bounded interval.
- `unknown` requires `interval is None`.
- `remainingRatio` maps to `[ratio, ratio]`; absence never maps to `[0, 0]`.
- stale preserves the last observed interval but is never admission-eligible.
- Provider/account/source identifiers must pass Loom's stable identifier validation and may not contain email or display identity.

- [ ] **Step 3: Add canonical evidence digesting**

Serialize with sorted keys and compact separators, excluding no semantic field:

```python
def observation_digest(value: ResourceObservation) -> str:
    payload = json.dumps(
        dataclasses.asdict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()
```

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/python -m pytest tests/test_resource_observation_2026_07_18.py -q
git add devkit/resource_observation.py tests/test_resource_observation_2026_07_18.py
git commit -m "feat: define external resource observations"
```

### Task 2: Implement the bounded OpenUsage Bar Unix-socket observer

**Files in Loom repository:**
- Create: `devkit/openusage_resource_observer.py`
- Create: `tests/test_openusage_resource_observer_2026_07_18.py`

- [ ] **Step 1: Write a fake-socket contract suite**

Test success, missing socket, connect timeout, response timeout, non-HTTP/1.1, body over 1 MiB, malformed JSON, wrong schema version, more than 1000 quota records, unknown fields, duplicate identities, stale facts, and revision changes between requests.

- [ ] **Step 2: Add a standard-library Unix connection**

```python
class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(self.socket_path))
        self.sock = sock
```

The observer uses a three-second total timeout, a 1 MiB response cap, `Connection: close`, schema `1.0`, a private user-owned socket, and at most 1000 capacity facts. It never follows redirects, reads SQLite, reads Keychain, or caches last-good data.

- [ ] **Step 3: Implement V0 revision-consistent reads**

Before `/v1/snapshot` is available, read `/v1/health`, `/v1/capacity`, and `/v1/sources/status`. Require one `dataRevision`; retry the complete group once if revisions differ. A second mismatch returns one sanitized `INCONSISTENT_REVISION` unknown observation result and no numeric intervals.

After OpenUsage Bar publishes `/v1/snapshot`, switch the observer to that single endpoint while retaining recorded V0 fixtures as compatibility tests.

- [ ] **Step 4: Map API facts without policy**

- `dataRevision` -> `source_revision`.
- `providerId/accountRef/quotaName` -> resource identity.
- `remainingRatio` -> exact interval.
- `resetsAt/observedAt` -> temporal facts.
- `quality=direct|authoritative` -> known.
- `quality=derived` -> estimated.
- missing or unsupported numeric data -> unknown.
- `stale=true` -> stale; retain the observation but mark it ineligible.
- API health only proves transport health; Provider availability comes from source state.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -m pytest tests/test_openusage_resource_observer_2026_07_18.py -q
git add devkit/openusage_resource_observer.py \
  tests/test_openusage_resource_observer_2026_07_18.py
git commit -m "feat: observe OpenUsage resource facts"
```

### Task 3: Publish the adapter boundary and prove observe-only behavior

**Files in Loom repository:**
- Create: `src/loom/adapters/resource/__init__.py`
- Create: `src/loom/adapters/resource/openusage.py`
- Modify: `src/loom/adapters/README.md`
- Create: `tests/test_openusage_observe_only_2026_07_18.py`

- [ ] **Step 1: Add the public compatibility import**

`src/loom/adapters/resource/openusage.py` re-exports only the supported observer, contract, error enum, and digest helper from the current `devkit` implementation. Do not place the observer in `src/loom/adapters/provider.py`; that module is an execution/provider compatibility surface.

- [ ] **Step 2: Write the authority-boundary test**

Instantiate an observer with a fake Unix API, replace Scheduler and ProviderRouter entry points with fail-on-call spies, collect observations, and assert both call counts remain zero. Assert the observer writes no OpenUsage file, Loom policy, Session state, capability, or route record.

- [ ] **Step 3: Document CURRENT/PARTIAL/TARGET**

Record:

- `CURRENT`: bounded observe-only adapter and normalized resource facts.
- `PARTIAL`: resource evidence can be displayed/reported but is not Session-bound.
- `TARGET`: policy-assisted routing after explicit authorization and live Canary.

- [ ] **Step 4: Run focused plus canonical tests**

```bash
.venv/bin/python -m pytest \
  tests/test_resource_observation_2026_07_18.py \
  tests/test_openusage_resource_observer_2026_07_18.py \
  tests/test_openusage_observe_only_2026_07_18.py -q
.venv/bin/python -m pytest tests/ -q
```

Expected: all tests pass and existing Scheduler/ProviderRouter outcomes remain unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/loom/adapters/resource src/loom/adapters/README.md \
  tests/test_openusage_observe_only_2026_07_18.py
git commit -m "feat: expose the OpenUsage resource observer"
```

### Task 4: Bind resource evidence to SessionBinding after its mainline prerequisite

**Prerequisite:** The selected Loom base contains reviewed `SessionBindingController`, `SessionEventRecorder.append_unique`, Event Journal replay, and Session read-model support. Do not copy those modules into an older branch only to satisfy this task.

**Files in Loom repository:**
- Modify: `devkit/session_binding_controller.py`
- Modify: `devkit/session_event_recorder.py`
- Modify: `devkit/session_read_model.py`
- Create: `tests/test_session_resource_observation_2026_07_18.py`

- [ ] **Step 1: Define the append-only event test**

The event shape is:

```json
{
  "kind": "SessionResourceObservationRecorded",
  "binding_ref": "binding-1",
  "child_id": "implementer-1",
  "run_id": "run-1",
  "attempt": 1,
  "evidence_ref": "evidence:openusage:v1:r4007:sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "normalized_observation": {
    "api_version": "loom.resource.observation/v1",
    "source": "openusage-bar",
    "source_revision": 4007,
    "generated_at": "2026-07-18T00:00:00Z",
    "provider_id": "codex",
    "account_ref": null,
    "quota_name": "weekly",
    "state": "known",
    "interval": [0.73, 0.73],
    "resets_at": "2026-07-20T00:00:00Z",
    "observed_at": "2026-07-18T00:00:00Z",
    "freshness": "live",
    "source_id": "current.quota",
    "quality": "direct"
  }
}
```

The test covers wrong binding/provider rejection, repeated event idempotency, restart replay, unknown facts, and stale facts. An observation event cannot change phase, issue Capability, call ProviderRouter, or satisfy Delivery.

- [ ] **Step 2: Add `record_resource_observation`**

Validate the active binding identity, observation schema, source revision, and digest. Construct a deterministic event ID from binding, attempt, revision, and digest. Append through `SessionEventRecorder.append_unique`; never mutate the immutable SessionBinding v1 payload.

- [ ] **Step 3: Project the observation read-only**

`session_read_model.py` exposes the latest observation and evidence reference under `binding.resource_observation`. Replay of the same Journal must produce byte-equivalent projection output.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/python -m pytest tests/test_session_resource_observation_2026_07_18.py -q
.venv/bin/python -m pytest tests/ -q
git add devkit/session_binding_controller.py devkit/session_event_recorder.py \
  devkit/session_read_model.py tests/test_session_resource_observation_2026_07_18.py
git commit -m "feat: bind resource observations as session evidence"
```

## Explicitly excluded from this implementation

- Automatic Provider selection or fallback.
- Mutation of Provider credentials or OpenUsage Bar configuration.
- Reading the OpenUsage Bar SQLite ledger directly.
- Loom-owned last-good caching of OpenUsage facts.
- Treating stale facts as admission-ready.
- Restarting Autopilot, enabling Apply, or changing current report-only authority.

## Acceptance gate

- Identical API facts produce an identical canonical digest.
- Unknown and unavailable facts never acquire a numeric interval.
- Stale observations remain visible but cannot drive admission or routing.
- The adapter stays bounded to three seconds, 1 MiB, schema v1, and 1000 facts.
- OpenUsage Bar data remains read-only and credentials never cross the API.
- Session evidence is append-only, idempotent, and replay-stable when Task 4 is enabled.
- Existing Scheduler and ProviderRouter results remain unchanged.
