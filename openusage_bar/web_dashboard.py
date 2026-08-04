"""Loopback-only read-only web dashboard for the UsageHub ledger.

The dashboard is deliberately minimal: a single HTML page rendered from the
same ``QueryService`` that backs the local API, plus a JSON snapshot route.
It binds to ``127.0.0.1`` only and never renders credentials, prompts, or
request payloads. The native menu bar and future desktop shells stay the
primary surfaces; this page exists for cross-platform and headless users.
"""

from __future__ import annotations

import html
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .query import QueryService, to_wire


DEFAULT_DASHBOARD_PORT = 17822


def _pill(state: str) -> str:
    kind = {
        "ok": "ok",
        "stale": "warn",
        "temporarily_unavailable": "warn",
        "error": "bad",
    }.get(state, "warn")
    return f'<span class="pill pill-{kind}">{html.escape(state)}</span>'


def render_dashboard(
    snapshot: dict[str, Any], today: str, model_summary: tuple[dict[str, Any], ...] = ()
) -> str:
    summary = snapshot.get("summary") or {}
    today_tokens = summary.get("todayTokens")
    model_count = summary.get("modelCount")
    covered_days = summary.get("coveredDayCount")
    providers = snapshot.get("providers", [])
    sources = snapshot.get("sources", [])
    quota_hub = snapshot.get("quotaHub", [])
    healthy_sources = sum(1 for item in sources if item.get("state") == "ok")
    token_value = "unknown" if today_tokens is None else format(int(today_tokens), ",")
    token_meta = (
        f"{model_count} 个模型 · {covered_days} 天覆盖"
        if model_count is not None
        else "暂无可聚合的 Token 事实"
    )
    model_rows = "".join(
        f"""
        <tr>
          <th scope="row">{html.escape(item["model"])}</th>
          <td class="mono dim">{html.escape(item["source"])}</td>
          <td class="mono">{html.escape(item["tokens"])}</td>
          <td class="mono">{html.escape(item["cost_label"])}</td>
        </tr>"""
        for item in model_summary
    ) or '<tr><td colspan="4" class="empty">近 7 天暂无模型用量</td></tr>'

    quota_rows = "".join(
        f"""
        <article class="quota">
          <div class="quota-bar" aria-hidden="true"></div>
          <div class="quota-body">
            <p class="quota-currency">{html.escape(item.get("currency") or "n/a")}</p>
            <p class="quota-amount mono">{html.escape(item.get("totalAvailable") or "n/a")}</p>
            <p class="quota-meta">{item.get("providerCount")} 个 Provider</p>
            <ul class="provenance">
              {''.join(
                  f"<li class=\"mono\">{html.escape(':'.join(part))}</li>"
                  for part in item.get("provenance", [])
              )}
            </ul>
          </div>
        </article>"""
        for item in quota_hub
    ) or '<p class="empty">暂无实测余额，接入余额接口后这里会出现聚合。</p>'

    provider_rows = "".join(
        f"""
        <tr>
          <th scope="row">{html.escape(item.get("providerId") or "n/a")}</th>
          <td>{html.escape(item.get("displayName") or "n/a")}</td>
          <td class="mono">{html.escape(item.get("sourceKind") or "n/a")}</td>
        </tr>"""
        for item in providers
    ) or '<tr><td colspan="3" class="empty">暂无 Provider</td></tr>'

    source_rows = "".join(
        f"""
        <tr>
          <th scope="row">{html.escape(item.get("providerId") or "n/a")}</th>
          <td class="mono">{html.escape(item.get("sourceId") or "n/a")}</td>
          <td>{_pill(item.get("state") or "unknown")}</td>
          <td class="mono dim">{html.escape(item.get("errorCode") or "")}</td>
        </tr>"""
        for item in sources
    ) or '<tr><td colspan="4" class="empty">暂无 Source</td></tr>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>UsageHub Dashboard</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #F7F7F5;
      --surface: #FFFFFF;
      --surface-alt: #F0F0EE;
      --text: #1A1B1E;
      --text-dim: #5B5F66;
      --text-faint: #6A707A;
      --hairline: #E3E4E1;
      --accent: #087F52;
      --accent-soft: rgba(8, 127, 82, 0.12);
      --warn: #B45309;
      --warn-soft: rgba(180, 83, 9, 0.12);
      --bad: #C0392B;
      --bad-soft: rgba(192, 57, 43, 0.12);
      --radius: 12px;
      --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
        "Liberation Mono", monospace;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0B0C0E;
        --surface: #121316;
        --surface-alt: #181A1E;
        --text: #E8EAED;
        --text-dim: #A7ADB8;
        --text-faint: #7B818C;
        --hairline: #23262C;
        --accent: #34D399;
        --accent-soft: rgba(52, 211, 153, 0.14);
        --warn: #FBBF24;
        --warn-soft: rgba(251, 191, 36, 0.14);
        --bad: #F87171;
        --bad-soft: rgba(248, 113, 113, 0.14);
      }}
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      font-family: var(--font-sans);
      background: var(--bg);
      color: var(--text);
      line-height: 1.55;
      padding: 28px clamp(18px, 4vw, 48px);
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }}
    .shell {{ max-width: 1120px; margin: 0 auto; }}
    header.top {{
      display: flex; align-items: center; justify-content: space-between;
      gap: 16px; flex-wrap: wrap; padding-bottom: 18px;
      border-bottom: 1px solid var(--hairline); margin-bottom: 22px;
    }}
    .brand {{ display: flex; align-items: center; gap: 12px; }}
    .brand-mark {{
      width: 30px; height: 30px; border-radius: 8px; color: var(--accent);
      background: var(--accent-soft); display: grid; place-items: center;
    }}
    .brand-mark svg {{ width: 18px; height: 18px; }}
    .brand h1 {{ font-size: 1.05rem; font-weight: 650; margin: 0; letter-spacing: -0.01em; }}
    .brand p {{ margin: 1px 0 0; font-size: 0.78rem; color: var(--text-dim); }}
    .health {{
      display: inline-flex; align-items: center; gap: 7px;
      font-size: 0.8rem; color: var(--text-dim); font-variant-numeric: tabular-nums;
    }}
    .dot {{ width: 7px; height: 7px; border-radius: 50%; background: var(--accent); }}
    .dot.off {{ background: var(--warn); }}
    .metrics {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 0; border: 1px solid var(--hairline); border-radius: var(--radius);
      background: var(--surface); overflow: hidden; margin-bottom: 26px;
    }}
    .metric {{ padding: 16px 18px; border-left: 1px solid var(--hairline); }}
    .metric:first-child {{ border-left: 0; }}
    .metric-label {{ margin: 0; font-size: 0.72rem; letter-spacing: 0.06em;
      text-transform: uppercase; color: var(--text-faint); }}
    .metric-value {{ margin: 5px 0 0; font-size: 1.55rem; font-weight: 650;
      font-family: var(--font-mono); font-variant-numeric: tabular-nums;
      letter-spacing: -0.02em; }}
    .metric-meta {{ margin: 4px 0 0; font-size: 0.75rem; color: var(--text-dim); }}
    h2.section {{ font-size: 0.95rem; font-weight: 650; margin: 0 0 12px;
      color: var(--text); }}
    .panel {{ border: 1px solid var(--hairline); border-radius: var(--radius);
      background: var(--surface); margin-bottom: 26px; overflow: hidden; }}
    .panel-head {{ display: flex; align-items: baseline; justify-content: space-between;
      padding: 13px 16px; border-bottom: 1px solid var(--hairline); }}
    .panel-head h3 {{ margin: 0; font-size: 0.82rem; font-weight: 600; }}
    .panel-head span {{ font-size: 0.75rem; color: var(--text-faint);
      font-variant-numeric: tabular-nums; }}
    .quota-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
      gap: 12px; padding: 14px; }}
    .quota {{ position: relative; border: 1px solid var(--hairline);
      border-radius: var(--radius); background: var(--surface-alt);
      padding: 15px 16px 15px 20px; overflow: hidden; }}
    .quota-bar {{ position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
      background: var(--accent); }}
    .quota-currency {{ margin: 0; font-size: 0.72rem; letter-spacing: 0.06em;
      text-transform: uppercase; color: var(--text-faint); }}
    .quota-amount {{ margin: 4px 0 0; font-size: 1.5rem; font-weight: 650;
      letter-spacing: -0.02em; }}
    .quota-meta {{ margin: 4px 0 0; font-size: 0.76rem; color: var(--text-dim); }}
    .provenance {{ list-style: none; margin: 10px 0 0; padding: 0;
      display: flex; flex-wrap: wrap; gap: 6px; }}
    .provenance li {{ font-size: 0.7rem; color: var(--text-dim);
      background: var(--surface); border: 1px solid var(--hairline);
      padding: 2px 8px; border-radius: 999px; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
    th, td {{ text-align: left; padding: 10px 16px; }}
    thead th {{ font-size: 0.72rem; letter-spacing: 0.05em; text-transform: uppercase;
      color: var(--text-faint); border-bottom: 1px solid var(--hairline); }}
    tbody tr {{ border-bottom: 1px solid var(--hairline); }}
    tbody tr:last-child {{ border-bottom: 0; }}
    th[scope="row"] {{ color: var(--text); font-weight: 600; white-space: nowrap; }}
    td {{ color: var(--text-dim); white-space: nowrap; }}
    .mono {{ font-family: var(--font-mono); font-variant-numeric: tabular-nums; }}
    .dim {{ color: var(--text-faint); }}
    .pill {{ display: inline-flex; align-items: center; gap: 5px;
      padding: 2px 9px; border-radius: 999px; font-size: 0.72rem; font-weight: 550; }}
    .pill::before {{ content: ""; width: 5px; height: 5px; border-radius: 50%;
      background: currentColor; }}
    .pill-ok {{ color: var(--accent); background: var(--accent-soft); }}
    .pill-warn {{ color: var(--warn); background: var(--warn-soft); }}
    .pill-bad {{ color: var(--bad); background: var(--bad-soft); }}
    .empty {{ color: var(--text-dim); padding: 16px; }}
    footer {{ margin-top: 26px; padding-top: 14px; border-top: 1px solid var(--hairline);
      color: var(--text-faint); font-size: 0.76rem; display: flex; align-items: center;
      gap: 7px; }}
    footer svg {{ width: 14px; height: 14px; }}
    :focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px;
      border-radius: 4px; }}
    @media (max-width: 640px) {{
      .metrics {{ grid-template-columns: 1fr 1fr; }}
      .metric:nth-child(3) {{ border-left: 0; }}
      .metric:nth-child(n+3) {{ border-top: 1px solid var(--hairline); }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      * {{ transition: none !important; animation: none !important; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header class="top">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 3v18h18"/>
            <path d="M7 15v-4M12 15V8M17 15v-7"/>
          </svg>
        </span>
        <div>
          <h1>UsageHub</h1>
          <p>本地优先，只读，数据留在本机</p>
        </div>
      </div>
      <span class="health">
        <span class="dot {'off' if healthy_sources == 0 else ''}" aria-hidden="true"></span>
        {healthy_sources} / {len(sources)} 数据源正常
      </span>
    </header>

    <section aria-label="关键指标" class="metrics">
      <div class="metric">
        <p class="metric-label">今日 Token</p>
        <p class="metric-value">{token_value}</p>
        <p class="metric-meta">{token_meta}</p>
      </div>
      <div class="metric">
        <p class="metric-label">Provider</p>
        <p class="metric-value">{len(providers)}</p>
        <p class="metric-meta">已接入实例</p>
      </div>
      <div class="metric">
        <p class="metric-label">实测余额</p>
        <p class="metric-value">{len(quota_hub)}</p>
        <p class="metric-meta">按币种聚合</p>
      </div>
      <div class="metric">
        <p class="metric-label">账本日期</p>
        <p class="metric-value">{html.escape(today)}</p>
        <p class="metric-meta">本地快照</p>
      </div>
    </section>

    <h2 class="section">Quota Hub</h2>
    <div class="quota-grid">{quota_rows}</div>

    <section class="panel" aria-label="Providers">
      <div class="panel-head">
        <h3>Providers</h3>
        <span>{len(providers)} 个实例</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th scope="col">Provider</th><th scope="col">名称</th><th scope="col">Source Kind</th></tr></thead>
          <tbody>{provider_rows}</tbody>
        </table>
      </div>
    </section>

    <section class="panel" aria-label="Sources 健康">
      <div class="panel-head">
        <h3>Sources 健康</h3>
        <span>{healthy_sources} 正常</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th scope="col">Provider</th><th scope="col">Source</th><th scope="col">状态</th><th scope="col">错误</th></tr></thead>
          <tbody>{source_rows}</tbody>
        </table>
      </div>
    </section>

    <section class="panel" aria-label="按模型消耗">
      <div class="panel-head">
        <h3>按模型消耗</h3>
        <span>近 7 天</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th scope="col">模型</th><th scope="col">来源</th><th scope="col">Token</th><th scope="col">费用</th></tr></thead>
          <tbody>{model_rows}</tbody>
        </table>
      </div>
    </section>

    <footer>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect x="3" y="11" width="18" height="11" rx="2"/>
        <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
      </svg>
      凭据只进系统钥匙串，账本与日志不含密钥
    </footer>
  </main>
</body>
</html>
"""


def make_dashboard_server(
    query: QueryService,
    *,
    port: int = DEFAULT_DASHBOARD_PORT,
    today: date | None = None,
) -> ThreadingHTTPServer:
    """Create the loopback dashboard server (caller owns serve/shutdown)."""

    def handler_factory(*args: Any, **kwargs: Any) -> BaseHTTPRequestHandler:
        return _DashboardHandler(query, today, *args, **kwargs)

    return ThreadingHTTPServer(("127.0.0.1", port), handler_factory)


class _DashboardHandler(BaseHTTPRequestHandler):
    server_version = "UsageHubDashboard/1"

    def __init__(
        self,
        query: QueryService,
        today: date | None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self._query = query
        self._today = today or date.today()
        super().__init__(*args, **kwargs)

    def _write(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        try:
            snapshot = to_wire(self._query.resource_snapshot(self._today))
        except Exception:
            self.send_error(500, "snapshot unavailable")
            return
        if self.path in {"/", "/index.html"}:
            self._write(
                render_dashboard(
                    snapshot,
                    self._today.isoformat(),
                    self._model_summary(),
                ).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return
        if self.path == "/v1/snapshot":
            import json

            body = json.dumps(snapshot, ensure_ascii=True, separators=(",", ":")).encode(
                "utf-8"
            )
            self._write(body, "application/json")
            return
        self.send_error(404)

    def _model_summary(self) -> tuple[dict[str, Any], ...]:
        """Aggregate last-7-day token and cost facts by (model, source)."""
        from decimal import Decimal, InvalidOperation

        start = self._today - timedelta(days=6)
        try:
            result = self._query.activity(start, self._today)
        except Exception:
            return ()
        aggregated: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in result.rows:
            currency = row.cost_currency or "USD"
            key = (row.model_id, row.source_id, currency)
            entry = aggregated.setdefault(
                key,
                {
                    "model": row.model_id,
                    "source": row.source_id,
                    "currency": currency,
                    "tokens": 0,
                    "cost": Decimal("0"),
                },
            )
            entry["tokens"] += row.total_tokens
            if row.cost_amount:
                try:
                    entry["cost"] += Decimal(str(row.cost_amount))
                except (InvalidOperation, ValueError):
                    pass
        ordered = sorted(
            aggregated.values(), key=lambda item: item["tokens"], reverse=True
        )[:12]
        rendered: list[dict[str, Any]] = []
        for item in ordered:
            cost = item["cost"]
            if cost:
                label = f"{format(cost, '.6f').rstrip('0').rstrip('.')} {item['currency']}"
            else:
                label = "n/a"
            rendered.append(
                {
                    "model": item["model"],
                    "source": item["source"],
                    "tokens": format(item["tokens"], ","),
                    "cost_label": label,
                }
            )
        return tuple(rendered)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return
