# Custom Daily Usage Feed boundary

## Why this exists

OpenUsage and provider-specific adapters remain the first choice. The custom
feed is only for a provider or internal gateway that already exposes a bounded,
range-aware HTTPS JSON usage endpoint but has no reusable OpenUsage adapter.

It is part of the independent OpenUsage Bar repository and does not patch or
embed OpenUsage.

## Supported declarative contract

- HTTPS GET or JSON POST;
- credentials from macOS Keychain in one configured header;
- required inclusive `since` and `until` request parameters;
- dotted paths for item list, date/time, model, Token components, total, and
  optional provider-reported cost;
- date, ISO-8601, Unix seconds, or Unix milliseconds with an IANA timezone;
- none, page, offset, or cursor pagination;
- a fixed, secret-free POST body.

The Settings UI exposes the common GET/no-pagination template. Advanced POST
and pagination fields use the same validated configuration schema and can be
added to the native form without changing importer semantics.

## Safety and correctness

- HTTP, embedded URL credentials, fragments, Cookie headers, redirects to
  another host, private-address SSRF, arbitrary scripts, JSONPath, and
  executable templates are rejected.
- Date-range parameters are mandatory so an empty response can represent
  covered zero instead of an ambiguous partial feed.
- Pagination is capped at 64 pages, 100,000 records, the bounded HTTP response
  size, and a 60-second operation deadline.
- Only mapped fields enter `DailyUsageRow`; raw payloads and identity fields are
  not persisted or logged.
- Token totals must equal the explicitly mapped components. If a provider
  reports cache as a subset of input, that cache path must be left unmapped
  until a future total-semantics option is configured.
- A malformed page, repeated cursor, auth failure, timeout, or mapping error
  returns no partial rows and preserves last-good ledger data.
- The winning source is `custom.daily_feed`; successful official/built-in
  sources still take precedence according to the collector's source policy.
