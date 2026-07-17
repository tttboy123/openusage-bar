# MiniMax daily model Token reuse decision

## Decision

Reuse the existing MiniMax subscription credential, bounded HTTP client, and the
platform billing feed used by `JochenYang/minimax-status`. Do not copy its CLI,
credential file, cache, presentation code, or dependencies.

MiniMax's documented Token Plan API remains the source for current 5-hour and
weekly capacity. The separate `https://www.minimaxi.com/account/amount` feed is
used only for delayed daily/model Token activity and is recorded under the
distinct source ID `minimax.billing`.

## Why this is experimental

The public Token Plan documentation documents `GET /v1/token_plan/remains`, but
does not document a daily billing API. The account feed is a platform-web
endpoint, so its importer must fail closed, keep last-good rows, and allow the
collector to cold-start from OpenUsage when the feed is unavailable.

Delete this importer when either OpenUsage or a documented MiniMax API provides
equivalent daily model Token facts.

## Accepted field projection

Only these fields are read from each billing record:

- `created_at` for the Asia/Shanghai calendar day;
- `model` for a bounded ledger-safe model ID;
- `consume_input_token`;
- `consume_output_token`;
- `consume_token`, which must equal input plus output.

The importer never persists or logs `mail`, `creator_id`, `creator_name`,
`group_id`, `api_token_name`, raw records, requests, prompts, or responses.

## Completeness and freshness

- Pagination is bounded to 100 pages and a 60-second operation deadline.
- Page totals and newest-to-oldest ordering are validated before commit.
- A partial or malformed response produces no rows.
- The current China calendar day is excluded because the billing feed is
  delayed; coverage ends at yesterday.
- Input, output, and total Token counts are aggregated by day and model.
