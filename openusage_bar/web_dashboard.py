"""Loopback-only read-only web dashboard for the UsageHub ledger.

The dashboard is deliberately minimal: a single HTML page rendered from the
same ``QueryService`` that backs the local API, plus a JSON snapshot route.
It binds to ``127.0.0.1`` only and never renders credentials, prompts, or
request payloads. The native menu bar and future desktop shells stay the
primary surfaces; this page exists for cross-platform and headless users.
"""

from __future__ import annotations

import html
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .query import QueryService, to_wire


DEFAULT_DASHBOARD_PORT = 17822


def _render_row(item: Any) -> str:
    return "<tr>" + "".join(
        f"<td>{html.escape(str(value))}</td>" for value in item
    ) + "</tr>"


def render_dashboard(snapshot: dict[str, Any], today: str) -> str:
    summary = snapshot.get("summary") or {}
    today_tokens = summary.get("todayTokens")
    model_count = summary.get("modelCount")
    covered_days = summary.get("coveredDayCount")
    providers = snapshot.get("providers", [])
    sources = snapshot.get("sources", [])
    quota_hub = snapshot.get("quotaHub", [])
    healthy_sources = sum(1 for item in sources if item.get("state") == "ok")
    token_value = "unknown" if today_tokens is None else format(int(today_tokens), ",")

    quota_cards = "".join(
        f"""
        <article class="card quota-card">
          <div class="quota-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
                 stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"/>
              <path d="M3 5v14a2 2 0 0 0 2 2h16v-5"/>
              <path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/>
            </svg>
          </div>
          <div>
            <p class="card-label">{html.escape(item.get("currency") or "—")} 可用额度</p>
            <p class="card-value mono">{html.escape(item.get("totalAvailable") or "—")}</p>
            <p class="card-meta">{item.get("providerCount")} 个 Provider</p>
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
          <th scope="row">{html.escape(item.get("providerId") or "—")}</th>
          <td>{html.escape(item.get("displayName") or "—")}</td>
          <td><code>{html.escape(item.get("sourceKind") or "—")}</code></td>
        </tr>"""
        for item in providers
    ) or '<tr><td colspan="3" class="empty">暂无 Provider</td></tr>'

    def _pill(state: str) -> str:
        kind = {
            "ok": "ok",
            "stale": "warn",
            "temporarily_unavailable": "warn",
            "error": "bad",
        }.get(state, "warn")
        return f'<span class="pill pill-{kind}">{html.escape(state)}</span>'

    source_rows = "".join(
        f"""
        <tr>
          <th scope="row">{html.escape(item.get("providerId") or "—")}</th>
          <td><code>{html.escape(item.get("sourceId") or "—")}</code></td>
          <td>{_pill(item.get("state") or "unknown")}</td>
          <td>{html.escape(item.get("errorCode") or "")}</td>
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
      --color-primary: #1E40AF;
      --color-on-primary: #FFFFFF;
      --color-secondary: #3B82F6;
      --color-accent: #D97706;
      --color-background: #F8FAFC;
      --color-foreground: #1E3A8A;
      --color-muted: #E9EEF6;
      --color-border: #DBEAFE;
      --color-destructive: #DC2626;
      --color-success: #15803D;
      --color-warn: #B45309;
      --color-card: #FFFFFF;
      --color-text: #1F2937;
      --color-text-secondary: #4B5563;
      --color-text-muted: #6B7280;
      --shadow-card: 0 1px 2px rgba(15, 23, 42, .05), 0 4px 12px rgba(15, 23, 42, .06);
      --shadow-card-hover: 0 2px 4px rgba(15, 23, 42, .07), 0 10px 24px rgba(30, 64, 175, .10);
      --radius: 14px;
      --font-sans: "Fira Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --font-mono: "Fira Code", ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --color-background: #0B1220;
        --color-foreground: #CBD5F5;
        --color-muted: #16213A;
        --color-border: #223252;
        --color-card: #111A2E;
        --color-text: #E5EAF5;
        --color-text-secondary: #A7B4D0;
        --color-text-muted: #8190B3;
        --color-success: #4ADE80;
        --color-warn: #FBBF24;
        --shadow-card: 0 1px 2px rgba(0,0,0,.4);
        --shadow-card-hover: 0 4px 16px rgba(59, 130, 246, .18);
      }}
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      font-family: var(--font-sans);
      background:
        radial-gradient(1200px 400px at 20% -10%, color-mix(in srgb, var(--color-secondary) 14%, transparent), transparent),
        var(--color-background);
      color: var(--color-text);
      line-height: 1.5;
      padding: 32px clamp(16px, 4vw, 48px);
      min-height: 100vh;
    }}
    .shell {{ max-width: 1080px; margin: 0 auto; }}
    header.top {{ display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
    .brand {{ display: flex; align-items: center; gap: 12px; }}
    .brand-mark {{
      display: grid; place-items: center; width: 44px; height: 44px; border-radius: 12px;
      background: linear-gradient(135deg, var(--color-primary), var(--color-secondary));
      color: var(--color-on-primary); box-shadow: var(--shadow-card-hover);
    }}
    .brand h1 {{ font-size: 1.35rem; margin: 0; color: var(--color-foreground); letter-spacing: -.01em; }}
    .brand p {{ margin: 2px 0 0; color: var(--color-text-muted); font-size: .85rem; }}
    .health {{ display: inline-flex; align-items: center; gap: 8px; font-size: .85rem; color: var(--color-text-secondary); }}
    .dot {{ width: 9px; height: 9px; border-radius: 50%; background: var(--color-success); animation: pulse 2s ease-in-out infinite; }}
    .dot.off {{ background: var(--color-warn); animation: none; }}
    @keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: .35; }} }}
    h2.section {{ font-size: 1rem; color: var(--color-foreground); margin: 28px 0 12px; display: flex; align-items: center; gap: 8px; }}
    .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; }}
    .card {{
      background: var(--color-card); border: 1px solid var(--color-border); border-radius: var(--radius);
      padding: 18px 20px; box-shadow: var(--shadow-card); transition: transform 160ms ease, box-shadow 160ms ease, border-color 160ms ease;
    }}
    .card:hover {{ transform: translateY(-1px); box-shadow: var(--shadow-card-hover); border-color: color-mix(in srgb, var(--color-secondary) 45%, var(--color-border)); }}
    .card-label {{ margin: 0; font-size: .8rem; color: var(--color-text-muted); text-transform: uppercase; letter-spacing: .05em; }}
    .card-value {{ margin: 4px 0 0; font-size: 1.7rem; font-weight: 650; color: var(--color-foreground); font-variant-numeric: tabular-nums; }}
    .card-value.mono {{ font-family: var(--font-mono); font-size: 1.35rem; }}
    .card-meta {{ margin: 4px 0 0; font-size: .82rem; color: var(--color-text-secondary); }}
    .quota-card {{ display: flex; gap: 14px; align-items: flex-start; }}
    .quota-icon {{ display: grid; place-items: center; width: 38px; height: 38px; border-radius: 10px; background: color-mix(in srgb, var(--color-accent) 14%, transparent); color: var(--color-accent); flex: none; }}
    .quota-icon svg, .brand-mark svg {{ width: 22px; height: 22px; }}
    .provenance {{ list-style: none; margin: 10px 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; }}
    .provenance li {{ font-size: .72rem; font-family: var(--font-mono); color: var(--color-text-secondary); background: var(--color-muted); padding: 2px 8px; border-radius: 999px; }}
    .table-card {{ padding: 6px 8px; }}
    .table-card .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
    caption {{ text-align: left; padding: 10px 12px; font-size: .8rem; color: var(--color-text-muted); }}
    th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--color-border); white-space: nowrap; }}
    tbody tr:last-child th, tbody tr:last-child td {{ border-bottom: 0; }}
    thead th {{ color: var(--color-text-muted); font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; }}
    th[scope="row"] {{ color: var(--color-foreground); font-weight: 600; }}
    code {{ font-family: var(--font-mono); font-size: .82rem; color: var(--color-secondary); }}
    .pill {{ display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: .75rem; font-weight: 550; }}
    .pill-ok {{ color: var(--color-success); background: color-mix(in srgb, var(--color-success) 12%, transparent); }}
    .pill-warn {{ color: var(--color-warn); background: color-mix(in srgb, var(--color-warn) 14%, transparent); }}
    .pill-bad {{ color: var(--color-destructive); background: color-mix(in srgb, var(--color-destructive) 12%, transparent); }}
    .empty {{ color: var(--color-text-muted); padding: 14px 12px; }}
    footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid var(--color-border); color: var(--color-text-muted); font-size: .8rem; display: flex; align-items: center; gap: 8px; }}
    footer svg {{ width: 16px; height: 16px; }}
    :focus-visible {{ outline: 2px solid var(--color-secondary); outline-offset: 2px; border-radius: 6px; }}
    @media (prefers-reduced-motion: reduce) {{
      * {{ animation: none !important; transition: none !important; }}
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
          <p>本地优先 · 只读 · 数据留在本机</p>
        </div>
      </div>
      <span class="health">
        <span class="dot {'off' if healthy_sources == 0 else ''}" aria-hidden="true"></span>
        {healthy_sources} / {len(sources)} 个数据源正常
      </span>
    </header>

    <section aria-label="关键指标">
      <div class="kpis">
        <article class="card">
          <p class="card-label">今日 Token</p>
          <p class="card-value">{token_value}</p>
          <p class="card-meta">{model_count} 个模型 · {covered_days} 天覆盖</p>
        </article>
        <article class="card">
          <p class="card-label">Provider</p>
          <p class="card-value">{len(providers)}</p>
          <p class="card-meta">已接入实例</p>
        </article>
        <article class="card">
          <p class="card-label">实测余额</p>
          <p class="card-value">{len(quota_hub)}</p>
          <p class="card-meta">按币种聚合（Quota Hub）</p>
        </article>
        <article class="card">
          <p class="card-label">覆盖日期</p>
          <p class="card-value mono">{html.escape(today)}</p>
          <p class="card-meta">本地账本快照</p>
        </article>
      </div>
    </section>

    <h2 class="section">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"/>
        <path d="M3 5v14a2 2 0 0 0 2 2h16v-5"/>
        <path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/>
      </svg>
      Quota Hub · 实测额度聚合
    </h2>
    <div class="kpis">{quota_cards}</div>

    <h2 class="section">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
        <path d="M3.3 7 12 12l8.7-5"/>
        <path d="M12 22V12"/>
      </svg>
      Providers
    </h2>
    <div class="card table-card">
      <div class="table-wrap">
        <table>
          <caption>已注册的 Provider 实例与数据源类型</caption>
          <thead><tr><th scope="col">Provider</th><th scope="col">名称</th><th scope="col">Source Kind</th></tr></thead>
          <tbody>{provider_rows}</tbody>
        </table>
      </div>
    </div>

    <h2 class="section">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M22 12h-4l-3 9L9 3l-3 9H2"/>
      </svg>
      Sources 健康
    </h2>
    <div class="card table-card">
      <div class="table-wrap">
        <table>
          <caption>本地数据源状态与最近错误</caption>
          <thead><tr><th scope="col">Provider</th><th scope="col">Source</th><th scope="col">状态</th><th scope="col">错误</th></tr></thead>
          <tbody>{source_rows}</tbody>
        </table>
      </div>
    </div>

    <footer>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect x="3" y="11" width="18" height="11" rx="2"/>
        <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
      </svg>
      凭据只进系统钥匙串 · 账本/日志不含密钥
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
                render_dashboard(snapshot, self._today.isoformat()).encode("utf-8"),
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

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return
