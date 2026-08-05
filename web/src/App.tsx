import { useEffect, useState } from "react";
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
  ChartLineUp as UsageIcon,
} from "@phosphor-icons/react";
import ActivityPage from "./pages/ActivityPage";
import CapacityPage from "./pages/CapacityPage";
import ApiSpendPage from "./pages/ApiSpendPage";
import LocalToolsPage from "./pages/LocalToolsPage";
import ProvidersPage from "./pages/ProvidersPage";
import DataHealthPage from "./pages/DataHealthPage";
import AutomationPage from "./pages/AutomationPage";
import UsageDetailsPage from "./pages/UsageDetailsPage";
import { fetchQuickConnect, type QuickConnectItem } from "./api";
import Reveal from "./components/Reveal";
import { detectLang, setLang, messages, type Lang, type Messages } from "./i18n";

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
  const t: Messages = messages[lang];

  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);

  useEffect(() => {
    void fetchQuickConnect().then(setQuick).catch(() => {});
  }, []);

  function switchLang() {
    const next: Lang = lang === "zh" ? "en" : "zh";
    setLang(next);
    setLangState(next);
  }

  return (
    <div className="shell">
      <aside className="sidebar">
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
              className={({ isActive }) =>
                `nav-link${isActive ? " active" : ""}`
              }
            >
              <Icon />
              {t[item.key]}
            </NavLink>
          );
        })}
      </aside>

      <main className="content">
        <header>
          <div>
            <h2>{t[TITLES[location.pathname] ?? "navActivity"]}</h2>
            <p className="sub">{t.localFirst}</p>
          </div>
          <div className="toolbar">
            <select
              className="icon-btn quick-connect"
              aria-label={t.quickConnect}
              defaultValue=""
              onChange={(event) => {
                if (event.target.value) {
                  window.open(event.target.value, "_blank", "noopener");
                  event.target.value = "";
                }
              }}
            >
              <option value="" disabled>
                {t.quickConnect}
              </option>
              {quick.map((item) => (
                <option key={item.familyId} value={item.consoleUrl}>
                  {item.familyId}
                </option>
              ))}
            </select>
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
              onClick={() => window.location.reload()}
            >
              <ArrowClockwise size={16} />
              {t.refresh}
            </button>
          </div>
        </header>

        <Reveal key={location.pathname}>
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
