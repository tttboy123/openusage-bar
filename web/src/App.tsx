import { useEffect, useRef, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import {
  ChartLineUp,
  Gauge,
  CurrencyDollar,
  Wrench,
  PlugsConnected,
  Heartbeat,
  Lightning,
  ChartBar,
  ArrowClockwise,
  ChartBar as UsageIcon,
  CaretDown,
  Globe,
} from "@phosphor-icons/react";
import ActivityPage from "./pages/ActivityPage";
import CapacityPage from "./pages/CapacityPage";
import ApiSpendPage from "./pages/ApiSpendPage";
import LocalToolsPage from "./pages/LocalToolsPage";
import ProvidersPage from "./pages/ProvidersPage";
import DataHealthPage from "./pages/DataHealthPage";
import AutomationPage from "./pages/AutomationPage";
import UsageDetailsPage from "./pages/UsageDetailsPage";
import {
  fetchQuickConnect,
  triggerRefresh,
  type QuickConnectItem,
} from "./api";
import Reveal from "./components/Reveal";
import { detectLang, setLang, messages, type Lang, type Messages } from "./i18n";
import { buildProductIdentityPresentation } from "./productBuildIdentity";
import { productVersionTruth } from "./productVersionTruth";

const NAV = [
  { to: "/activity", key: "navActivity", icon: ChartLineUp },
  { to: "/usage-details", key: "navUsageDetails", icon: UsageIcon },
  { to: "/capacity", key: "navCapacity", icon: Gauge },
  { to: "/api-spend", key: "navApiSpend", icon: CurrencyDollar },
  { to: "/local-tools", key: "navLocalTools", icon: Wrench },
  { to: "/providers", key: "navProviders", icon: PlugsConnected },
  { to: "/data-health", key: "navDataHealth", icon: Heartbeat },
  { to: "/automation", key: "navAutomation", icon: Lightning },
] as const;

const TITLES: Record<string, keyof Messages> = {
  "/activity": "navActivity",
  "/usage-details": "navUsageDetails",
  "/capacity": "navCapacity",
  "/api-spend": "navApiSpend",
  "/local-tools": "navLocalTools",
  "/providers": "navProviders",
  "/data-health": "navDataHealth",
  "/automation": "navAutomation",
};

export default function App() {
  const location = useLocation();
  const [lang, setLangState] = useState<Lang>(() => detectLang());
  const [quick, setQuick] = useState<QuickConnectItem[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const activeNavRef = useRef<HTMLAnchorElement>(null);
  const refreshInFlightRef = useRef(false);
  const t: Messages = messages[lang];
  const buildIdentity = buildProductIdentityPresentation(productVersionTruth, t);

  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);

  useEffect(() => {
    void fetchQuickConnect().then(setQuick).catch(() => {});
  }, []);

  useEffect(() => {
    if (location.pathname === "/capacity") void refreshAll();
    // Entering Capacity is the explicit active-refresh gesture. Other page
    // navigation remains a read-only ledger fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  useEffect(() => {
    activeNavRef.current?.scrollIntoView({ block: "nearest", inline: "center" });
  }, [location.pathname]);

  useEffect(() => {
    if (!menuOpen) return;
    const first = document.querySelector('[role="menuitem"]:not(:disabled)') as HTMLElement | null;
    first?.focus();
  }, [menuOpen]);

  useEffect(() => {
    if (!menuOpen) return;
    function onClickOutside(event: MouseEvent) {
      if (!menuRef.current || menuRef.current.contains(event.target as Node)) return;
      setMenuOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setMenuOpen(false);
    }
    window.addEventListener("mousedown", onClickOutside);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("mousedown", onClickOutside);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  async function refreshAll() {
    if (refreshInFlightRef.current) return;
    refreshInFlightRef.current = true;
    setRefreshing(true);
    setRefreshError(false);
    try {
      const requested = await triggerRefresh();
      if (requested.phase !== "idle" || requested.state !== "ok" || requested.succeeded !== true) {
        throw new Error("refresh failed");
      }
      setRefreshNonce((n) => n + 1);
    } catch {
      setRefreshError(true);
    } finally {
      refreshInFlightRef.current = false;
      setRefreshing(false);
    }
  }

  function switchLang() {
    const next: Lang = lang === "zh" ? "en" : "zh";
    setLang(next);
    setLangState(next);
  }

  return (
    <div className="shell">
      <nav className="sidebar" aria-label={t.primaryNavigation}>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <ChartBar size={20} />
          </span>
          <div>
            <h1>UsageHub</h1>
            <p>{t.localFirst}</p>
          </div>
        </div>
        {NAV.map((item) => {
          const Icon = item.icon;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              ref={item.to === location.pathname ? activeNavRef : undefined}
              className={({ isActive }) =>
                `nav-link${isActive ? " active" : ""}`
              }
            >
              <Icon />
              {t[item.key]}
            </NavLink>
          );
        })}
        <section className="build-identity" aria-label={t.buildIdentityLabel}>
          <strong>{buildIdentity.versionAndBuild}</strong>
          <span>{buildIdentity.lifecycle}</span>
          <span>{buildIdentity.published}</span>
          <span>{buildIdentity.canary}</span>
        </section>
      </nav>

      <main className="content">
        <header>
          <div>
            <h2>{t[TITLES[location.pathname] ?? "navActivity"]}</h2>
            <p className="sub">{t.localFirst}</p>
          </div>
          <div className="toolbar">
            <div className="dropdown" ref={menuRef}>
              <button
                type="button"
                className="icon-btn quick-connect"
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((v) => !v)}
              >
                <Globe size={16} />
                <span>{t.quickConnect}</span>
                <CaretDown size={14} />
              </button>
              {menuOpen ? (
                <div
                  className="dropdown-menu"
                  role="menu"
                  aria-label={t.quickConnect}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") {
                      event.preventDefault();
                      setMenuOpen(false);
                      return;
                    }
                    const items = Array.from(
                      event.currentTarget.querySelectorAll<HTMLElement>('[role="menuitem"]:not(:disabled)'),
                    );
                    if (items.length === 0) return;
                    const index = items.indexOf(document.activeElement as HTMLElement);
                    if (event.key === "ArrowDown") {
                      event.preventDefault();
                      items[Math.min(items.length - 1, index + 1)]?.focus();
                    } else if (event.key === "ArrowUp") {
                      event.preventDefault();
                      items[Math.max(0, index - 1)]?.focus();
                    } else if (event.key === "Home") {
                      event.preventDefault();
                      items[0]?.focus();
                    } else if (event.key === "End") {
                      event.preventDefault();
                      items[items.length - 1]?.focus();
                    }
                  }}>
                  {quick.length === 0 ? (
                    <button type="button" disabled>
                      {t.noMatchingPresets}
                    </button>
                  ) : (
                    quick.map((item) => (
                      <button
                        key={item.familyId}
                        type="button"
                        role="menuitem"
                        onClick={() => {
                          window.open(item.consoleUrl, "_blank", "noopener,noreferrer");
                          setMenuOpen(false);
                        }}
                      >
                        {item.familyId}
                      </button>
                    ))
                  )}
                </div>
              ) : null}
            </div>
            <button
              type="button"
              className="lang-switch"
              onClick={switchLang}
              aria-label={t.switchLanguage}
            >
              {lang === "zh" ? "EN" : "中文"}
            </button>
            <button
              type="button"
              className="icon-btn"
              onClick={() => void refreshAll()}
              disabled={refreshing}
              aria-busy={refreshing}
            >
              <ArrowClockwise size={16} className={refreshing ? "spinning" : ""} />
              {refreshing ? t.refreshing : t.refresh}
            </button>
            {refreshError ? (
              <span className="refresh-feedback" role="status" aria-live="polite">
                {t.refreshFailed}
              </span>
            ) : null}
          </div>
        </header>

        <Reveal key={`${location.pathname}-${refreshNonce}`}>
          <Routes>
            <Route path="/" element={<ActivityPage t={t} />} />
            <Route path="/activity" element={<ActivityPage t={t} />} />
            <Route path="/usage-details" element={<UsageDetailsPage t={t} />} />
            <Route path="/capacity" element={<CapacityPage t={t} />} />
            <Route path="/api-spend" element={<ApiSpendPage t={t} />} />
            <Route path="/local-tools" element={<LocalToolsPage t={t} />} />
            <Route path="/providers" element={<ProvidersPage t={t} />} />
            <Route path="/data-health" element={<DataHealthPage t={t} />} />
            <Route path="/automation" element={<AutomationPage t={t} />} />
          </Routes>
        </Reveal>
      </main>
    </div>
  );
}
