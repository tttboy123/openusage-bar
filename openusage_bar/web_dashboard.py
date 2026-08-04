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
    rows: list[str] = []
    rows.append(
        _render_row(
            (
                today,
                "unknown" if today_tokens is None else str(today_tokens),
                summary.get("modelCount", "unknown"),
                summary.get("coveredDayCount", "unknown"),
            )
        )
    )
    quota_rows = "".join(
        _render_row(
            (
                item.get("currency"),
                item.get("totalAvailable"),
                item.get("providerCount"),
                ", ".join(":".join(part) for part in item.get("provenance", [])),
            )
        )
        for item in snapshot.get("quotaHub", [])
    )
    provider_rows = "".join(
        _render_row(
            (
                item.get("providerId"),
                item.get("displayName"),
                item.get("sourceKind"),
            )
        )
        for item in snapshot.get("providers", [])
    )
    source_rows = "".join(
        _render_row(
            (
                item.get("providerId"),
                item.get("sourceId"),
                item.get("state"),
                item.get("errorCode") or "",
            )
        )
        for item in snapshot.get("sources", [])
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>UsageHub Dashboard</title>
  <style>
    body {{ font-family: -apple-system, "Segoe UI", sans-serif; margin: 2rem; color: #1c1c1e; }}
    table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
    th, td {{ border: 1px solid #d1d1d6; padding: .4rem .6rem; text-align: left; }}
    th {{ background: #f2f2f7; }}
  </style>
</head>
<body>
  <h1>UsageHub</h1>
  <h2>今日概览</h2>
  <table>
    <tr><th>日期</th><th>今日 Token</th><th>模型数</th><th>覆盖天数</th></tr>
    {rows[0]}
  </table>
  <h2>Quota Hub（实测余额聚合）</h2>
  <table>
    <tr><th>币种</th><th>可用总额</th><th>Provider 数</th><th>Provenance</th></tr>
    {quota_rows or "<tr><td colspan=4>暂无实测余额</td></tr>"}
  </table>
  <h2>Providers</h2>
  <table>
    <tr><th>Provider</th><th>名称</th><th>Source Kind</th></tr>
    {provider_rows or "<tr><td colspan=3>暂无 Provider</td></tr>"}
  </table>
  <h2>Sources 健康</h2>
  <table>
    <tr><th>Provider</th><th>Source</th><th>状态</th><th>错误</th></tr>
    {source_rows or "<tr><td colspan=4>暂无 Source</td></tr>"}
  </table>
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
