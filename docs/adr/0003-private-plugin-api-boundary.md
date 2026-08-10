# ADR 0003: Independent private Plugin API boundary

- Status: accepted
- Date: 2026-08-10

## Decision

UsageHub exposes external-tool integration through a third authenticated
loopback service, `plugin.openusage/v1` on `127.0.0.1:17824`. It does not add
routes to Local API v1 or Gateway API v1. Bearer tokens bind principals directly;
Desktop receives only aggregate connection-read scope.

The service reads facts through Local API and route advice through Gateway. It
does not open the activity ledger, Gateway stores, or Provider credential
stores. Plugin idempotency and decisions use a dedicated private
`plugin.sqlite3` with a seven-day, 10,000-record-per-principal promise window.
Decision/outcome mutation and exact idempotency response commit atomically.

## Consequences

Observer and Gateway can start, stop, and fail independently of Plugin
integrations. External adapters never receive Local API, Gateway, or Provider
credentials. The extra listener and token registry add lifecycle work, but make
principal isolation, least privilege, and failure containment testable.

Connection state is deliberately conservative: configured, connected,
capability-negotiated, and last-sync are separate facts. None implies tool
installation, Provider/account health, valid quota, or Gateway execution.
