 import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
 import { Link, useSearchParams } from "react-router-dom";
 import { ArrowClockwise, CaretDown } from "@phosphor-icons/react";
 import {
   fetchObserverPlatformCapability,
   fetchPluginConnections,
   fetchProviders,
   fetchQuickConnect,
   fetchRuntimeCapability,
   fetchSources,
   type ProviderItem,
   type QuickConnectItem,
   type SourceItem,
 } from "../api";
 import Skeleton from "../components/Skeleton";
 import {
   dataHealthCapabilityViewModel,
   type DataHealthCapabilityViewModel,
 } from "../dataHealthCapabilityViewModel";
 import {
   dataHealthCauseKey,
   dataHealthEmptyState,
   dataHealthSourceKey,
   dataHealthStatePresentation,
   type DataHealthCauseKey,
   type DataHealthStatePresentation,
 } from "../dataHealthViewState";
 import { tpl, type Messages } from "../i18n";
 import {
   observerPlatformViewModel,
   type ObserverPlatformCapability,
   type ObserverPlatformTone,
 } from "../observerPlatformCapability";
 import type { RuntimeCapability } from "../runtimeCapability";
 import {
   type PluginCapability,
   type PluginCapabilityState,
   type PluginConfiguration,
   type PluginConnection,
   type PluginConnections,
   type PluginConnectionState,
   type PluginId,
   type PluginSyncOutcome,
 } from "../pluginConnections";
 import { pluginHealthViewState } from "../pluginHealthViewState";
 import { serviceStatusViewModel } from "../serviceStatusViewModel";

 function formatTime(iso: string | null | undefined, t: Messages): string {
   if (!iso) return t.never;
   const date = new Date(iso);
   if (Number.isNaN(date.getTime())) return iso;
   return date.toLocaleString();
 }

 type StatusTone =
   | DataHealthCapabilityViewModel["observer"]["tone"]
   | ObserverPlatformTone;

 function pillClass(tone: StatusTone): string {
   if (tone === "positive") return "pill-ok";
   if (tone === "warning") return "pill-warn";
   if (tone === "negative") return "pill-bad";
   if (tone === "unknown") return "pill-unknown";
   return "pill-neutral";
 }

 type DataHealthRowModel = {
   item: SourceItem;
   snapshotIndex: number;
   issue: boolean;
   causeKey: DataHealthCauseKey;
   presentation: DataHealthStatePresentation;
 };

 const PLUGIN_NAMES: Record<PluginId, string> = {
   loom: "Loom",
   codex: "Codex",
   claude_code: "Claude Code",
 };
 const PLUGIN_CONFIGURATION_KEYS: Record<PluginConfiguration, keyof Messages> = {
   configured: "pluginConfigurationConfigured",
   not_configured: "pluginConfigurationNotConfigured",
   unknown: "pluginConfigurationUnknown",
 };
 const PLUGIN_CONNECTION_KEYS: Record<PluginConnectionState, keyof Messages> = {
   never_seen: "pluginConnectionNeverSeen",
   connected: "pluginConnectionConnected",
   stale: "pluginConnectionStale",
   unknown: "pluginConnectionUnknown",
 };
 const PLUGIN_CAPABILITY_STATE_KEYS: Record<PluginCapabilityState, keyof Messages> = {
   not_negotiated: "pluginCapabilityNotNegotiated",
   negotiated: "pluginCapabilityNegotiated",
   incompatible: "pluginCapabilityIncompatible",
   unknown: "pluginCapabilityUnknown",
 };
 const PLUGIN_CAPABILITY_KEYS: Record<PluginCapability, keyof Messages> = {
   "usage.query": "pluginCapabilityUsageQuery",
   "quotas.query": "pluginCapabilityQuotasQuery",
   "health.query": "pluginCapabilityHealthQuery",
   "route.advice": "pluginCapabilityRouteAdvice",
   "outcome.record": "pluginCapabilityOutcomeRecord",
   "decision.lookup": "pluginCapabilityDecisionLookup",
 };
 const PLUGIN_SYNC_KEYS: Record<PluginSyncOutcome, keyof Messages> = {
   never: "pluginSyncNever",
   succeeded: "pluginSyncSucceeded",
   failed: "pluginSyncFailed",
   unknown: "pluginSyncUnknown",
 };

 function pluginConfigurationTone(value: PluginConfiguration): string {
   return value === "unknown" ? "pill-unknown" : "pill-neutral";
 }

 function pluginConnectionTone(value: PluginConnectionState): string {
   if (value === "connected") return "pill-ok";
   if (value === "stale") return "pill-warn";
   if (value === "unknown") return "pill-unknown";
   return "pill-neutral";
 }

 function pluginCapabilityTone(value: PluginCapabilityState): string {
   if (value === "incompatible") return "pill-bad";
   if (value === "unknown") return "pill-unknown";
   return "pill-neutral";
 }

 function pluginSyncTone(value: PluginSyncOutcome): string {
   if (value === "succeeded") return "pill-ok";
   if (value === "failed") return "pill-bad";
   if (value === "unknown") return "pill-unknown";
   return "pill-neutral";
 }

 function PluginConnectionRow({
   connection,
   t,
 }: {
   connection: PluginConnection;
   t: Messages;
 }) {
   return (
     <li className="plugin-connection-item">
       <h4>{PLUGIN_NAMES[connection.pluginId]}</h4>
       <dl className="plugin-connection-facts">
         <div>
           <dt>{t.pluginConfiguration}</dt>
           <dd className="plugin-connection-value">
             <span className={`pill ${pluginConfigurationTone(connection.configuration)}`}>
               {t[PLUGIN_CONFIGURATION_KEYS[connection.configuration]]}
             </span>
           </dd>
         </div>
         <div>
           <dt>{t.pluginConnection}</dt>
           <dd className="plugin-connection-value">
             <span className={`pill ${pluginConnectionTone(connection.connection)}`}>
               {t[PLUGIN_CONNECTION_KEYS[connection.connection]]}
             </span>
             {connection.lastSeenAt !== null ? (
               <span className="plugin-connection-time">
                 {t.pluginLastSeen}{" "}
                 <time dateTime={connection.lastSeenAt}>
                   {formatTime(connection.lastSeenAt, t)}
                 </time>
               </span>
             ) : null}
           </dd>
         </div>
         <div>
           <dt>{t.pluginCapabilities}</dt>
           <dd className="plugin-connection-value">
             <span className={`pill ${pluginCapabilityTone(connection.capabilityState)}`}>
               {t[PLUGIN_CAPABILITY_STATE_KEYS[connection.capabilityState]]}
             </span>
             {connection.capabilities.length > 0 ? (
               <ul className="plugin-capability-list" aria-label={t.pluginCapabilities}>
                 {connection.capabilities.map((capability) => (
                   <li key={capability}>{t[PLUGIN_CAPABILITY_KEYS[capability]]}</li>
                 ))}
               </ul>
             ) : null}
           </dd>
         </div>
         <div>
           <dt>{t.pluginRecentSync}</dt>
           <dd className="plugin-connection-value">
             <span className={`pill ${pluginSyncTone(connection.lastSyncOutcome)}`}>
               {t[PLUGIN_SYNC_KEYS[connection.lastSyncOutcome]]}
             </span>
             {connection.lastSyncAt !== null ? (
               <time
                 className="plugin-connection-time"
                 dateTime={connection.lastSyncAt}
               >
                 {formatTime(connection.lastSyncAt, t)}
               </time>
             ) : null}
           </dd>
         </div>
       </dl>
     </li>
   );
 }

 function DataHealthRow({
   row,
   t,
   open,
   quickConnect,
   onToggle,
 }: {
   row: DataHealthRowModel;
   t: Messages;
   open: boolean;
   quickConnect?: QuickConnectItem;
   onToggle: () => void;
 }) {
   const detailId = useId();
   const { item, causeKey, presentation } = row;

   return (
     <li className={`health-item${open ? " open" : ""}`}>
       <button
         type="button"
         className="health-head"
         aria-expanded={open}
         aria-controls={detailId}
         onClick={onToggle}
       >
         <span className="health-title">
           {item.providerId ?? "n/a"}
           {item.sourceId ? (
             <span className="mono dim">{item.sourceId}</span>
           ) : null}
         </span>
         <span style={{ flex: 1 }} />
         <span className={`pill pill-${presentation.kind}`}>
           {t[presentation.labelKey]}
         </span>
         <CaretDown
           className="health-chevron"
           size={14}
           aria-hidden="true"
         />
       </button>
       <div
         className="health-detail"
         id={detailId}
         hidden={!open}
       >
         <div>
           <div className="health-inner">
             <p className="health-cause">{t[causeKey]}</p>
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
                   rel="noopener noreferrer"
                 >
                   {t.openConsole}
                 </a>
               ) : null}
             </div>
           </div>
         </div>
       </div>
     </li>
   );
 }

 export default function DataHealthPage({ t }: { t: Messages }) {
   const [capability, setCapability] = useState<RuntimeCapability | null>(null);
   const [observerPlatform, setObserverPlatform] =
     useState<ObserverPlatformCapability | null>(null);
   const [capabilityLoaded, setCapabilityLoaded] = useState(false);
   const [capabilityRefreshing, setCapabilityRefreshing] = useState(false);
   const [capabilityError, setCapabilityError] = useState(false);
   const [pluginConnections, setPluginConnections] =
     useState<PluginConnections | null>(null);
   const [pluginLoaded, setPluginLoaded] = useState(false);
   const [pluginRefreshing, setPluginRefreshing] = useState(false);
   const [pluginError, setPluginError] = useState(false);
   const [sources, setSources] = useState<SourceItem[]>([]);
   const [providers, setProviders] = useState<ProviderItem[]>([]);
   const [quick, setQuick] = useState<QuickConnectItem[]>([]);
   const [expanded, setExpanded] = useState<Set<string>>(new Set());
   const [filter, setFilter] = useState<"all" | "issues">("all");
   const [loading, setLoading] = useState(true);
   const [refreshing, setRefreshing] = useState(false);
   const [error, setError] = useState(false);
   const [searchParams, setSearchParams] = useSearchParams();
   const allSourcesButtonRef = useRef<HTMLButtonElement>(null);
   const pluginActionRef = useRef<HTMLButtonElement>(null);

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
       setError(false);
     } catch {
       setError(true);
     } finally {
       setLoading(false);
       setRefreshing(false);
     }
   }, []);

   async function refreshCapability() {
     setCapabilityRefreshing(true);
     try {
       const [runtimeResult, observerPlatformResult] = await Promise.all([
         fetchRuntimeCapability().then(
           (value) => ({ ok: true as const, value }),
           () => ({ ok: false as const }),
         ),
         fetchObserverPlatformCapability().then(
           (value) => ({ ok: true as const, value }),
           () => ({ ok: false as const }),
         ),
       ]);
       const nextCapability = runtimeResult.ok ? runtimeResult.value : null;
       const nextObserverPlatform = observerPlatformResult.ok
         ? observerPlatformResult.value
         : null;
       if (nextCapability !== null) setCapability(nextCapability);
       if (nextObserverPlatform !== null) {
         setObserverPlatform(nextObserverPlatform);
       }
       if (nextCapability === null || nextObserverPlatform === null) {
         setCapabilityError(true);
       } else {
         setCapabilityError(false);
       }
     } finally {
       setCapabilityLoaded(true);
       setCapabilityRefreshing(false);
     }
   }

   async function refreshPluginStatus(restoreFocus = false) {
     setPluginRefreshing(true);
     try {
       const next = await fetchPluginConnections();
       if (next === null) {
         setPluginError(true);
       } else {
         setPluginConnections(next);
         setPluginError(false);
       }
     } catch {
       setPluginError(true);
     } finally {
       setPluginLoaded(true);
       setPluginRefreshing(false);
       if (restoreFocus) {
         requestAnimationFrame(() => pluginActionRef.current?.focus());
       }
     }
   }

   useEffect(() => {
     void load();
   }, [load]);

   useEffect(() => {
     void refreshCapability();
   }, []);

   useEffect(() => {
     void refreshPluginStatus();
   }, []);

   const providerByFamily = useMemo(
     () => new Map(providers.map((p) => [p.providerId, p.familyId])),
     [providers],
   );
   const quickByFamily = useMemo(
     () => new Map(quick.map((q) => [q.familyId, q])),
     [quick],
   );

   const rows = useMemo<DataHealthRowModel[]>(
     () => sources.map((item, snapshotIndex) => {
       const presentation = dataHealthStatePresentation(item);
       return {
         item,
         snapshotIndex,
         issue: presentation.kind !== "ok",
         causeKey: dataHealthCauseKey(item),
         presentation,
       };
     }),
     [sources],
   );

   const issueCount = useMemo(
     () => rows.filter((row) => row.issue).length,
     [rows],
   );
   const emptyState = dataHealthEmptyState(sources.length, issueCount, filter);

   const visible = useMemo(() => {
     const sorted = [...rows].sort((a, b) => {
       const aIssue = a.issue ? 0 : 1;
       const bIssue = b.issue ? 0 : 1;
       return aIssue - bIssue;
     });
     return filter === "issues" ? sorted.filter((row) => row.issue) : sorted;
   }, [rows, filter]);

   // The opaque row key is shared by deep links, expansion state, and React.
   useEffect(() => {
     const deepLink = searchParams.get("source");
     if (!deepLink) return;
     const match = rows.find(
       (row) => dataHealthSourceKey(row.item, row.snapshotIndex) === deepLink,
     );
     if (match) {
       const key = dataHealthSourceKey(match.item, match.snapshotIndex);
       setExpanded((current) => {
         const next = new Set(current);
         next.add(key);
         return next;
       });
     }
   }, [searchParams, rows]);

   function toggle(row: DataHealthRowModel) {
     const key = dataHealthSourceKey(row.item, row.snapshotIndex);
     setExpanded((current) => {
       const next = new Set(current);
       if (next.has(key)) {
         next.delete(key);
       } else {
         next.add(key);
       }
       return next;
     });
     if (expanded.has(key)) {
       setSearchParams({}, { replace: true });
     } else {
       setSearchParams({ source: key }, {
         replace: true,
       });
     }
   }

   function refresh() {
     setRefreshing(true);
     void load();
   }

   function showAllSources() {
     setFilter("all");
     requestAnimationFrame(() => allSourcesButtonRef.current?.focus());
   }

   const capabilityModel = dataHealthCapabilityViewModel(capability);
   const observerPlatformModel = observerPlatformViewModel(observerPlatform);
   const observerPlatformCount =
     observerPlatformModel.supportedSourceCount === null ||
     observerPlatformModel.totalSourceCount === null
       ? t[observerPlatformModel.detailKey]
       : tpl(t[observerPlatformModel.detailKey], {
           supported: observerPlatformModel.supportedSourceCount,
           total: observerPlatformModel.totalSourceCount,
         });
   const providerHealth = capabilityModel.providerHealth;
   const capabilityLoading = !capabilityLoaded;
   const serviceStatus = serviceStatusViewModel({
     checking: capabilityLoading || capabilityRefreshing,
     failed: capabilityError,
     hasSnapshot:
       capabilityLoading || capability !== null || observerPlatform !== null,
   });
   const pluginStatus = pluginHealthViewState({
     checking: !pluginLoaded || pluginRefreshing,
     failed: pluginError,
     hasSnapshot: pluginConnections !== null,
   });

   const serviceSnapshot = serviceStatus.showSnapshot ? (
     <>
       <section
         className="panel"
         aria-labelledby="data-health-observer-title"
         aria-busy={capabilityRefreshing}
       >
         <div className="panel-head">
           <h3 id="data-health-observer-title">{t.observerTitle}</h3>
           <span>{t.serviceStatus}</span>
         </div>
         <div className="panel-body">
           {capabilityLoading ? (
             <Skeleton lines={7} />
           ) : (
             <>
               <dl className="automation-grid" style={{ margin: 0 }}>
                 <div className="automation-row">
                   <dt className="automation-label">
                     {t.capabilityOperational}
                   </dt>
                   <dd className="automation-value" style={{ margin: 0 }}>
                     <span
                       className={`pill ${pillClass(capabilityModel.observer.tone)}`}
                     >
                       {t[capabilityModel.observer.operationalKey]}
                     </span>
                   </dd>
                 </div>
                 <div className="automation-row">
                   <dt className="automation-label">{t.observerAccess}</dt>
                   <dd className="automation-value" style={{ margin: 0 }}>
                     {t.observerReadOnly}
                   </dd>
                 </div>
               </dl>
               <section
                 className="observer-platform-evidence"
                 aria-labelledby="observer-platform-evidence-title"
               >
                 <div className="observer-platform-heading">
                   <h4
                     className="section-title"
                     id="observer-platform-evidence-title"
                   >
                     {t.observerPlatformTitle}
                   </h4>
                   <span className="observer-platform-name">
                     {t[observerPlatformModel.operatingSystemKey]}
                   </span>
                 </div>
                 <dl className="observer-platform-facts">
                   <div>
                     <dt>{t.capabilityOperational}</dt>
                     <dd>
                       <span
                         className={`pill ${pillClass(observerPlatformModel.tone)}`}
                       >
                         {t[observerPlatformModel.stateKey]}
                       </span>
                     </dd>
                   </div>
                   <div>
                     <dt>{t.sources}</dt>
                     <dd className="observer-platform-count">
                       {observerPlatformCount}
                     </dd>
                   </div>
                 </dl>
               </section>
               <p className="panel-hint" style={{ marginTop: 14 }}>
                 {t.localFirst}
               </p>
             </>
           )}
         </div>
       </section>

       <section
         className="panel"
         aria-labelledby="data-health-gateway-title"
         aria-busy={capabilityRefreshing}
       >
         <div className="panel-head">
           <h3 id="data-health-gateway-title">{t.gatewayTitle}</h3>
           <span>{t.gatewayOptional}</span>
         </div>
         <div className="panel-body">
           {capabilityLoading ? (
             <Skeleton lines={8} />
           ) : (
             <>
               <dl className="automation-grid" style={{ margin: 0 }}>
                 <div className="automation-row">
                   <dt className="automation-label">{t.modeLabel}</dt>
                   <dd className="automation-value" style={{ margin: 0 }}>
                     {t[capabilityModel.gateway.modeKey]}
                   </dd>
                 </div>
                 <div className="automation-row">
                   <dt className="automation-label">
                     {t.capabilityOperational}
                   </dt>
                   <dd className="automation-value" style={{ margin: 0 }}>
                     <span
                       className={`pill ${pillClass(capabilityModel.gateway.tone)}`}
                     >
                       {t[capabilityModel.gateway.overallOperationalKey]}
                     </span>
                   </dd>
                 </div>
               </dl>

               <p className="panel-hint" style={{ marginTop: 14 }}>
                 {t[capabilityModel.gateway.consequenceKey]}
               </p>

               <section
                 aria-labelledby="gateway-provider-health-title"
                 style={{ marginTop: 18 }}
               >
                 <h4
                   className="section-title"
                   id="gateway-provider-health-title"
                 >
                   {t[providerHealth.titleKey]}
                 </h4>
                 <p className="panel-hint">
                   <span className={`pill ${pillClass(providerHealth.tone)}`}>
                     {tpl(
                       t[providerHealth.summaryKey],
                       providerHealth.summaryValues,
                     )}
                   </span>
                 </p>
                 <dl
                   className="automation-grid"
                   style={{ margin: "12px 0 0" }}
                 >
                   <div className="automation-row">
                     <dt className="automation-label">
                       {t.configuredProviders}
                     </dt>
                     <dd className="automation-value mono" style={{ margin: 0 }}>
                       {providerHealth.configuredCountText}
                     </dd>
                   </div>
                   <div className="automation-row">
                     <dt className="automation-label">{t.healthyProviders}</dt>
                     <dd className="automation-value mono" style={{ margin: 0 }}>
                       {providerHealth.healthyCountText}
                     </dd>
                   </div>
                 </dl>
               </section>

             </>
           )}
         </div>
       </section>
     </>
   ) : null;

   return (
     <>
       <div className="service-status-strip">
         <p className="service-status-message">
           {serviceStatus.messageKey
             ? t[serviceStatus.messageKey]
             : t.serviceStatus}
         </p>
         <p
           className="service-status-announcement"
           role="status"
           aria-live="polite"
           aria-atomic="true"
         >
           <span>{t[serviceStatus.announcementKey]}</span>{" "}
           <span>{t[pluginStatus.announcementKey]}</span>
         </p>
         <button
           type="button"
           className="icon-btn service-status-action"
           onClick={() => void refreshCapability()}
           disabled={serviceStatus.actionDisabled}
           aria-busy={serviceStatus.actionBusy}
         >
           <ArrowClockwise
             size={16}
             className={`service-status-spinner${
               serviceStatus.actionBusy ? " spinning" : ""
             }`}
             aria-hidden="true"
           />
           {t[serviceStatus.actionKey]}
         </button>
       </div>

       {serviceSnapshot}

       <section
         className="panel plugin-connections-panel"
         aria-labelledby="plugin-connections-title"
         aria-describedby="plugin-connections-hint"
         aria-busy={pluginStatus.actionBusy}
       >
         <div className="panel-head">
           <h3 id="plugin-connections-title">{t.pluginConnectionsTitle}</h3>
           <span>{t.gatewayOptional}</span>
         </div>
         <div className="panel-body">
           <p id="plugin-connections-hint" className="plugin-connections-hint">
             {t.pluginConnectionsHint}
           </p>
           <div className="plugin-connections-toolbar">
             <p className="plugin-connections-message">
               {pluginStatus.messageKey
                 ? t[pluginStatus.messageKey]
                 : pluginConnections !== null
                   ? tpl(t.pluginConnectionsObservedAt, {
                       time: formatTime(pluginConnections.observedAt, t),
                     })
                   : t.pluginConnectionsUnavailable}
             </p>
             <button
               ref={pluginActionRef}
               type="button"
               className="icon-btn plugin-connections-action"
               onClick={() => void refreshPluginStatus(true)}
               disabled={pluginStatus.actionDisabled}
               aria-busy={pluginStatus.actionBusy}
             >
               <ArrowClockwise
                 size={16}
                 className={`plugin-connections-spinner${
                   pluginStatus.actionBusy ? " spinning" : ""
                 }`}
                 aria-hidden="true"
               />
               {t[pluginStatus.actionKey]}
             </button>
           </div>
         </div>
         {!pluginLoaded && pluginConnections === null ? (
           <div className="panel-body plugin-connections-loading">
             <Skeleton lines={5} />
           </div>
         ) : pluginStatus.showSnapshot && pluginConnections !== null ? (
           <ul className="plugin-connection-list">
             {pluginConnections.connections.map((connection) => (
               <PluginConnectionRow
                 key={connection.pluginId}
                 connection={connection}
                 t={t}
               />
             ))}
           </ul>
         ) : null}
       </section>

       <section className="panel" aria-labelledby="observer-sources-title">
       <div className="panel-head">
         <h3 id="observer-sources-title">{t.observerSources}</h3>
         <span>
           {issueCount > 0 ? `${issueCount}/${sources.length} ` : ""}
           {t.sources}
         </span>
       </div>
       <div className="panel-body">
         {!loading && (!error || sources.length > 0) && sources.length > 0 ? (
           <p className="health-summary" role="status">
             {issueCount === 0
               ? tpl(t.allCollecting, { count: sources.length })
               : tpl(t.needAttention, { issue: issueCount, total: sources.length })}
           </p>
         ) : null}
         <div className="health-toolbar">
           <button
             ref={allSourcesButtonRef}
             type="button"
             className={`icon-btn filter-btn${filter === "all" ? " active" : ""}`}
             aria-pressed={filter === "all"}
             onClick={() => setFilter("all")}
           >
             {t.allSources}
           </button>
           <button
             type="button"
             className={`icon-btn filter-btn${filter === "issues" ? " active" : ""}`}
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
             <ArrowClockwise size={16} className={refreshing ? "spinning" : ""} />
             {refreshing ? t.refreshing : t.refreshStatus}
           </button>
         </div>
       </div>

       {loading ? (
         <Skeleton lines={6} />
       ) : (
         <>
           {emptyState === "no_sources" && !error ? (
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
           ) : emptyState === "no_issues" ? (
             <div className="panel-body">
               <p className="empty">
                 {t.noSourceIssues}{" "}
                 <button
                   type="button"
                   className="btn-link"
                   onClick={showAllSources}
                 >
                   {t.showAllSources}
                 </button>
               </p>
             </div>
           ) : visible.length > 0 ? (
             <ul className="health-list">
               {visible.map((row) => {
                 const key = dataHealthSourceKey(row.item, row.snapshotIndex);
                 const open = expanded.has(key);
                 const familyId = providerByFamily.get(
                   row.item.providerId ?? "",
                 );
                 const quickConnect = quickByFamily.get(familyId ?? "");
                 return (
                   <DataHealthRow
                     key={key}
                     row={row}
                     t={t}
                     open={open}
                     quickConnect={quickConnect}
                     onToggle={() => toggle(row)}
                   />
                 );
               })}
             </ul>
           ) : null}
           {error ? (
             <div className="panel-body">
               <p className="empty" role="status">
                 {t.healthLoadError}{" "}
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
     </>
   );
 }
