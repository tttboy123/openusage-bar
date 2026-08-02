# Community Smart Routing Research

- Status: Complete for architecture selection
- Date: 2026-08-02
- Scope: Official documentation, official repositories and primary papers
- Product constraint: OpenUsage Bar remains local-first and works without Loom

## Executive conclusion

OpenUsage Bar should not begin by cloning a universal proxy. Its unique asset is
the local fact plane: capacity, balance, cost, source health, provenance,
freshness and recent runtime observations across Provider accounts. The first
routing product should therefore be a content-free, explainable Decision API.
An OpenAI-compatible proxy is a separate, opt-in second phase.

No surveyed project combines all of the following:

- local subscription capacity and reset windows;
- multiple accounts per Provider;
- strict missing-versus-zero and stale-data semantics;
- recent request latency, failure and Token velocity;
- session-level reserve awareness;
- a standalone macOS human interface and a stable machine interface.

The recommended synthesis is:

| Need | Primary references | OpenUsage Bar use |
| --- | --- | --- |
| Deterministic gateway control | LiteLLM, Portkey, Bifrost, MLflow | hard filters, health, retry/fallback graph, budget policy |
| Learned strong/weak selection | RouteLLM, FrugalGPT | later optional scorer behind deterministic eligibility |
| Session/run budget | SeqRoute | advisory reserve and bankruptcy-risk signal |
| Online adaptation | OrcaRouter | later shadow-only contextual bandit, never the initial authority |
| Evaluation | TensorZero, RouterArena | replay, shadow decisions, counterfactual comparison |
| Self-hosted inference pools | Envoy AI Gateway, Gateway API Inference Extension | a separate future lane below Provider/model granularity |

## Product and gateway comparison

| Project | Shape | Useful routing signals | Reliability and budget behavior | Local/self-hosted | Migration decision |
| --- | --- | --- | --- | --- | --- |
| LiteLLM Router | Python SDK and OpenAI-compatible proxy | health, TPM/RPM, latency, cost, tags, region and custom checks | cooldowns, retries, fallbacks, timeouts, weighted/least-busy/latency/cost strategies | yes | Reuse concepts and existing OpenUsage LiteLLM producer; do not make LiteLLM a Phase A dependency. |
| Portkey AI Gateway | universal gateway with composable JSON strategies | request metadata, Provider/model, guardrail result, cache and policy | nested conditional routing, load balancing, fallbacks, rate and budget limits | OSS gateway and self-host options | Borrow composable policy graphs and explicit fallback reasons. |
| Bifrost | high-performance OSS gateway and UI | Provider/key performance, cache and policy | load balancing, automatic failover, semantic cache and budget management | yes | Strong Phase B reference; do not embed before the Decision API contract is stable. |
| MLflow AI Gateway | database-backed gateway inside MLflow | Provider, usage metrics, traffic split and policy | traffic splitting, fallback chains and budget alerts/limits | yes | Borrow policy/revision/governance UI patterns. |
| RouteLLM | learned router framework and compatible server | prompt features and preference-trained strong-model win rate | calibrated threshold trades cost for quality; not a high-availability gateway | yes | Later learned scorer baseline; never bypass capability, privacy, health or quota filters. |
| OpenRouter Auto | hosted router and relay | task type, model ranking, Provider price/latency and data policy | Provider sorting and fallback; charged as selected model | no | Product comparator only. `openrouter/auto` is deprecated in favor of `auto-beta` as of this research date. |
| Not Diamond | hosted pre-trained and custom learned routers | evaluation data, responses, scores, cost and latency preference | chooses a model by learned performance; infrastructure failover is not its core | no | Borrow custom-router training workflow, not the service dependency. |
| Martian | hosted gateway and quality-prediction product | predicted quality under cost constraints | public router internals and budget DSL are limited | no | Conceptual reference only. |
| TensorZero | self-hosted LLMOps gateway, experiments and evaluations | metrics, feedback, variants and evaluation data | retries, fallbacks, A/B and adaptive experimentation | yes | Borrow replay/evaluation discipline and immutable policy versions. |
| Cloudflare AI Gateway | hosted edge gateway | errors, timeouts, cache and rate policy | retry, fallback, cache and rate limiting | no | Borrow step-level fallback trace semantics, not the hosted dependency. |
| Helicone AI Gateway | OSS/self-host gateway | uptime, rate limit, latency, cost and weights | reliable/fastest/cheapest routing, rate and budget limits | yes | Useful Phase B implementation reference; license must be rechecked before code reuse. |
| Envoy AI Gateway | Kubernetes/Envoy gateway | model route, token rate limit and endpoint metrics | two-tier global gateway plus inference-pool control | yes | Future self-hosted inference lane only. |
| Gateway API Inference Extension | Kubernetes inference-routing standard | KV-cache locality, queue depth, availability and adapters | endpoint picker and inference-pool load balancing | yes | Track as a portability standard; avoid importing Kubernetes complexity into the macOS MVP. |
| Apache APISIX AI Gateway | plugin-based AI-aware gateway | Token usage, instances and caller/model scope | retry, fallback, load balancing and token rate limiting | yes | Borrow plugin boundary and token-aware limiting concepts. |
| Unify | product surface has shifted toward AI teammates/local deployment | current public routing contract cannot be verified | current public routing behavior is not stable enough to depend on | mixed | Historical reference only. |

## Research and evaluation comparison

| Work | Main idea | Appropriate adoption point |
| --- | --- | --- |
| FrugalGPT | cascade from cheaper models while preserving a quality target | deterministic or evaluated escalation after Phase A facts are stable |
| RouteLLM | learned strong-versus-weak routing calibrated by a threshold | first optional learned scorer |
| RouterArena | standardized router comparison and leaderboard | replay/evaluation harness before live learned routing |
| SeqRoute | session/global-budget-aware routing as a sequential decision problem | advisory session reserve in Phase A; learned policy only much later |
| OrcaRouter | contextual bandit combining offline and online evidence with operational penalties | shadow-only experiments after reliable counterfactual logs exist |
| RouterWise and latency-aware routing | deployment state and dynamic workload matter more than a static model latency | future local inference and deployment-level candidate model |

## Requirements derived for OpenUsage Bar

### Phase A: Decision API

1. Perform hard eligibility checks before scoring.
2. Default to reliability-first deterministic scoring.
3. Treat Provider, account, model and deployment as distinct target levels.
4. Preserve Unknown, stale, partial and unsupported facts; never substitute zero.
5. Return selected, alternative and rejected candidates with stable reason codes.
6. Include the exact policy version, fact revisions and decision expiry.
7. Accept no Prompt, Response, messages, tools, headers, credentials or raw payload.
8. Support an optional anonymous session/run budget without claiming an atomic reservation.
9. Provide replayable, content-free fixtures and deterministic tie-breaking.
10. Keep the existing Resource API read-only and on its current socket.

### Phase B: optional proxy

1. Run only after explicit opt-in and use a separate authenticated loopback surface.
2. Keep request content in memory and out of facts, telemetry and decision history.
3. Use the Decision API rather than implementing a second policy engine.
4. Retry/fallback only within a bounded attempt graph.
5. Never transparently switch after streaming output has started.
6. Keep external Provider routing and self-hosted inference-pool routing as separate target adapters.

### Evaluation before learned authority

- frozen replay fixtures;
- shadow decisions with no request effect;
- quality/cost/latency and no-route metrics;
- counterfactual comparisons and policy regret;
- explicit promotion and rollback gates;
- no raw request content in the routing ledger.

## Rejected product directions

- A hosted routing relay as the product core.
- A learned prompt classifier as the first routing authority.
- Adding POST or proxy behavior to the existing read-only Resource API.
- Treating a stale or unknown capacity value as unlimited capacity.
- Summing official facts, OpenUsage fallback and Runtime estimates.
- Combining SaaS Provider routing and local inference replica routing into one
  untyped target abstraction.

## Primary sources

- [LiteLLM Router documentation](https://docs.litellm.ai/docs/routing)
- [LiteLLM budget routing](https://docs.litellm.ai/docs/proxy/provider_budget_routing)
- [Portkey AI Gateway](https://portkey.ai/docs/product/ai-gateway)
- [Portkey fallbacks](https://portkey.ai/docs/product/ai-gateway/fallbacks)
- [Portkey OSS gateway](https://github.com/portkey-ai/gateway)
- [RouteLLM repository](https://github.com/lm-sys/RouteLLM)
- [RouteLLM paper](https://arxiv.org/abs/2406.18665)
- [FrugalGPT paper](https://arxiv.org/abs/2305.05176)
- [OpenRouter Auto Router](https://openrouter.ai/docs/guides/routing/routers/auto-router)
- [OpenRouter Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Not Diamond routing overview](https://docs.notdiamond.ai/docs/what-is-model-routing)
- [Not Diamond custom router training](https://docs.notdiamond.ai/docs/router-training-quickstart)
- [TensorZero repository](https://github.com/tensorzero/tensorzero)
- [TensorZero evaluations](https://www.tensorzero.com/docs/evaluations/)
- [Cloudflare AI Gateway](https://developers.cloudflare.com/ai-gateway/)
- [Helicone AI Gateway](https://github.com/Helicone/ai-gateway)
- [Bifrost](https://github.com/maximhq/bifrost)
- [Envoy AI Gateway](https://github.com/envoyproxy/ai-gateway)
- [Gateway API Inference Extension](https://gateway-api-inference-extension.sigs.k8s.io/)
- [MLflow AI Gateway](https://mlflow.org/docs/latest/genai/governance/ai-gateway/)
- [Apache APISIX AI Gateway](https://apisix.apache.org/ai-gateway/)
- [RouterArena paper](https://arxiv.org/abs/2510.00202)
- [SeqRoute paper](https://arxiv.org/html/2605.25424v1)
- [OrcaRouter paper](https://arxiv.org/html/2605.30736v1)
- [RouterWise paper](https://arxiv.org/html/2604.10907)
- [Latency-aware routing paper](https://arxiv.org/html/2607.18253v1)

All time-sensitive product statements above were checked on 2026-08-02. They
must be refreshed before adopting an external dependency or copying licensed
source code.
