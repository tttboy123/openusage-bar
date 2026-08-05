import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ArrowClockwise, CaretDown } from "@phosphor-icons/react";
import {
  fetchProviders,
  fetchQuickConnect,
  fetchSources,
  type ProviderItem,
  type QuickConnectItem,
  type SourceItem,
} from "../api";
import Skeleton from "../components/Skeleton";
import { tpl, type Messages } from "../i18n";

function isIssue(item: SourceItem): boolean {
  return !["ok", "available"].includes((item.state ?? "").toLowerCase());
}

function stateKind(state?: string): "ok" | "warn" | "bad" {
  const value = (state ?? "").toLowerCase();
  if (value === "ok" || value === "available") return "ok";
  if (value === "error") return "bad";
  return "warn";
}

function sourceKey(item: SourceItem): string {
  return `${item.providerId ?? "unknown"}/${item.sourceId ?? "unknown"}`;
}

function causeFor(item: SourceItem, t: Messages): string {
  const state = (item.state ?? "").toLowerCase();
  const code = (item.errorCode ?? "").toLowerCase();
  if (state === "ok") return t.healthOk;
  if (code === "empty_result") return t.healthEmptyResult;
  if (code === "timeout") return t.healthTimeout;
  if (
    code === "unauthorized" ||
    code === "invalid_credentials" ||
    code === "auth_expired" ||
    code === "401"
  ) {
    return t.healthUnauthorized;
  }
  if (state === "stale") return t.healthStale;
  if (state === "temporarily_unavailable") return t.healthTemporarily;
  if (state === "error") return t.healthError;
  return tpl(t.healthUnknown, { code: item.errorCode ?? state ?? "unknown" });
}

function formatTime(iso: string | null | undefined, t: Messages): string {
  if (!iso) return t.never;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString();
}

export default function DataHealthPage({ t }: { t: Messages }) {
  const [sources, setSources] = useState<SourceItem[]>([]);
  const [providers, setProviders] = useState<ProviderItem[]>([]);
  const [quick, setQuick] = useState<QuickConnectItem[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<"all" | "issues">("all");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();

  const load = useCallback(async () => {
    try {
      const [nextSources, nextProviders, nextQuick] = await Promise.all([
        fetchSources(),
        fetchProviders(),
        fetchQuickConnect(),
      ]);
      setSources(nextSources);
      setProviders(nextProviders);
      setQuick(nextQuick);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const providerByFamily = useMemo(
    () => new Map(providers.map((p) => [p.providerId, p.familyId])),
    [providers],
  );
  const quickByFamily = useMemo(
    () => new Map(quick.map((q) => [q.familyId, q])),
    [quick],
  );

  const issueCount = useMemo(
    () => sources.filter(isIssue).length,
    [sources],
  );

  const visible = useMemo(() => {
    const sorted = [...sources].sort((a, b) => {
      const aIssue = isIssue(a) ? 0 : 1;
      const bIssue = isIssue(b) ? 0 : 1;
      return aIssue - bIssue;
    });
    return filter === "issues" ? sorted.filter(isIssue) : sorted;
  }, [sources, filter]);

  // Deep link: ?source=<providerId>/<sourceId> opens that source's detail.
  useEffect(() => {
    const deepLink = searchParams.get("source");
    if (!deepLink) return;
    const match = sources.find((item) => sourceKey(item) === deepLink);
    if (match) {
      setExpanded((current) => {
        const next = new Set(current);
        next.add(sourceKey(match));
        return next;
      });
    }
  }, [searchParams, sources]);

  function toggle(item: SourceItem) {
    const key = sourceKey(item);
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
    const provider = item.providerId ?? "";
    if (expanded.has(key)) {
      setSearchParams({}, { replace: true });
    } else {
      setSearchParams({ source: `${provider}/${item.sourceId ?? ""}` }, {
        replace: true,
      });
    }
  }

  function refresh() {
    setRefreshing(true);
    void load();
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <h3>{t.navDataHealth}</h3>
        <span>
          {issueCount > 0 ? `${issueCount}/${sources.length} ` : ""}
          {t.sources}
        </span>
      </div>
      <div className="panel-body">
        <p className="health-summary" role="status">
          {issueCount === 0
            ? tpl(t.allCollecting, { count: sources.length })
            : tpl(t.needAttention, { issue: issueCount, total: sources.length })}
        </p>
        <div className="health-toolbar">
          <button
            type="button"
            className="icon-btn"
            aria-pressed={filter === "all"}
            onClick={() => setFilter("all")}
          >
            {t.allSources}
          </button>
          <button
            type="button"
            className="icon-btn"
            aria-pressed={filter === "issues"}
            onClick={() => setFilter("issues")}
          >
            {t.issuesOnly}
            {issueCount > 0 ? ` (${issueCount})` : ""}
          </button>
          <span style={{ flex: 1 }} />
          <button
            type="button"
            className="icon-btn"
            onClick={refresh}
            disabled={refreshing}
          >
            <ArrowClockwise size={16} />
            {refreshing ? t.refreshing : t.refreshStatus}
          </button>
        </div>
      </div>

      {loading ? (
        <Skeleton lines={6} />
      ) : (
        <>
          {sources.length === 0 && !error ? (
            <div className="panel-body">
              <p className="empty">
                {t.healthNoSources}{" "}
                <button
                  type="button"
                  className="btn-link"
                  onClick={refresh}
                  disabled={refreshing}
                >
                  {t.retry}
                </button>
              </p>
            </div>
          ) : (
            <ul className="health-list">
              {visible.map((item) => {
                const key = sourceKey(item);
                const open = expanded.has(key);
                const familyId = providerByFamily.get(item.providerId ?? "");
                const quickConnect = quickByFamily.get(familyId ?? "");
                return (
                  <li
                    key={key}
                    className={`health-item${open ? " open" : ""}`}
                  >
                    <button
                      type="button"
                      className="health-head"
                      aria-expanded={open}
                      aria-controls={`health-detail-${key}`}
                      onClick={() => toggle(item)}
                    >
                      <span className="health-title">
                        {item.providerId ?? "n/a"}
                        {item.sourceId ? (
                          <span className="mono dim">{item.sourceId}</span>
                        ) : null}
                      </span>
                      <span style={{ flex: 1 }} />
                      <span
                        className={`pill pill-${stateKind(item.state)}`}
                      >
                        {item.state ?? "unknown"}
                      </span>
                      <CaretDown
                        className="health-chevron"
                        size={14}
                        aria-hidden="true"
                      />
                    </button>
                    {open ? (
                      <div
                        className="health-detail"
                        id={`health-detail-${key}`}
                      >
                        <p className="health-cause">{causeFor(item, t)}</p>
                        <dl className="health-facts">
                          <div>
                            <dt>{t.lastAttempt}</dt>
                            <dd>{formatTime(item.lastAttemptAt, t)}</dd>
                          </div>
                          <div>
                            <dt>{t.lastSuccess}</dt>
                            <dd>{formatTime(item.lastSuccessAt, t)}</dd>
                          </div>
                          <div>
                            <dt>{t.staleAfter}</dt>
                            <dd>{formatTime(item.staleAt, t)}</dd>
                          </div>
                        </dl>
                        <div className="health-actions">
                          <Link className="btn-link" to="/providers">
                            {t.fixProvider}
                          </Link>
                          {quickConnect?.consoleUrl ? (
                            <a
                              className="btn-link"
                              href={quickConnect.consoleUrl}
                              target="_blank"
                              rel="noopener"
                            >
                              {t.openConsole}
                            </a>
                          ) : null}
                        </div>
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
          {error ? (
            <div className="panel-body">
              <p className="empty">
                {error}{" "}
                <button
                  type="button"
                  className="btn-link"
                  onClick={refresh}
                  disabled={refreshing}
                >
                  {t.retry}
                </button>
              </p>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
