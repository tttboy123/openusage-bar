import { useEffect, useState } from "react";
import { Plus, HardDrives, ArrowClockwise } from "@phosphor-icons/react";
import {
  fetchProviders,
  fetchSources,
  fetchQuickConnect,
  type ProviderItem,
  type SourceItem,
  type QuickConnectItem,
} from "../api";
import AddProviderDialog from "../components/AddProviderDialog";
import ProviderCard from "../components/ProviderCard";
import { type Messages, tpl } from "../i18n";

export default function ProvidersPage({ t }: { t: Messages }) {
  const [providers, setProviders] = useState<ProviderItem[]>([]);
  const [sources, setSources] = useState<SourceItem[]>([]);
  const [quick, setQuick] = useState<QuickConnectItem[]>([]);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

  const load = async (quiet = false) => {
    if (!quiet) setIsRefreshing(true);
    try {
      const [p, s, q] = await Promise.all([
        fetchProviders(),
        fetchSources(),
        fetchQuickConnect(),
      ]);
      setProviders(p);
      setSources(s);
      setQuick(q);
      setError(null);
      setLastRefreshed(new Date());
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    } finally {
      if (!quiet) setIsRefreshing(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    const id = setInterval(() => load(true), 60000);
    return () => clearInterval(id);
  }, []);

  const sourcesByProvider = new Map(
    sources.map((s) => [s.providerId, s] as const),
  );
  const quickByFamily = new Map(quick.map((q) => [q.familyId, q]));

  return (
    <>
      <div className="toolbar" style={{ marginBottom: 14 }}>
        <button
          type="button"
          className="primary-btn"
          onClick={() => setDialogOpen(true)}
        >
          <Plus size={16} />
          {t.addConnection}
        </button>
        <div className="toolbar-right">
          <span className="dim">
            {lastRefreshed
              ? tpl(t.lastUpdated, { time: relativeTime(lastRefreshed.toISOString(), t) })
              : t.refreshStatus}
          </span>
          <button
            type="button"
            className="icon-btn"
            onClick={() => load()}
            disabled={isRefreshing}
            aria-label={t.refresh}
            title={t.refresh}
          >
            <ArrowClockwise size={16} className={isRefreshing ? "spinning" : ""} />
          </button>
        </div>
      </div>
      <AddProviderDialog
        open={dialogOpen}
        presets={quick}
        onClose={() => setDialogOpen(false)}
        t={t}
      />
      <section className="provider-grid" aria-label={t.navProviders}>
        {providers.map((item) => (
         <ProviderCard
           key={item.providerId}
           providerId={item.providerId ?? ""}
           displayName={item.displayName ?? item.providerId ?? "n/a"}
           familyId={item.familyId ?? item.providerId ?? ""}
           sourceKind={item.sourceKind}
           source={sourcesByProvider.get(item.providerId ?? "")}
           quick={quickByFamily.get(item.familyId ?? "")}
           t={t}
            isSyncing={isRefreshing}
         />
        ))}
        {providers.length === 0 && !error ? (
          <div className="empty-block" style={{ gridColumn: "1 / -1" }}>
            <HardDrives size={32} />
            <p>{t.comingSoon}</p>
          </div>
        ) : null}
      </section>
      {error ? <p className="empty">{error}</p> : null}
    </>
  );
}

function relativeTime(date: string | null | undefined, t: Messages): string {
  if (!date) return t.never;
  const then = new Date(date).getTime();
  if (Number.isNaN(then)) return t.never;
  const minutes = Math.floor((Date.now() - then) / 60000);
  if (minutes < 1) return t.justNow ?? "just now";
  if (minutes < 60) return tpl(t.minutesAgo, { count: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return tpl(t.hoursAgo, { count: hours });
  const days = Math.floor(hours / 24);
  return tpl(t.daysAgo, { count: days });
}
