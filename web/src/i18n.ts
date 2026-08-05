export type Lang = "en" | "zh";

export const messages = {
  en: {
    navActivity: "Activity",
    navCapacity: "Capacity",
    navApiSpend: "API Spend",
    navLocalTools: "Local Tools",
    navProviders: "Providers",
    navDataHealth: "Data Health",
    navAutomation: "Automation",
    navUsageDetails: "Usage Details",
    todayTokens: "Today Tokens",
    providers: "Providers",
    balance: "Balance",
    ledgerDate: "Ledger Date",
    localFirst: "Local-first, read-only, data stays on this device",
    quotaHub: "Quota Hub",
    modelTrend: "Model Trend",
    modelSpend: "Spend by Model",
    noModelTrend:
      "No model activity matches the current filters. Switch the period or Provider to see a trend.",
    noSpend: "No model usage in the last 7 days.",
    refresh: "Refresh",
    openConsole: "Open Console",
    getApiKey: "Get API Key",
    connected: "connected",
    addConnection: "Add Connection",
    settings: "Settings",
    comingSoon: "This surface ships with the UsageHub v0.8 cross-platform client.",
  },
  zh: {
    navActivity: "活动",
    navCapacity: "额度",
    navApiSpend: "API 消耗",
    navLocalTools: "本地工具",
    navProviders: "Provider",
    navDataHealth: "数据健康",
    navAutomation: "自动化",
    navUsageDetails: "用量详情",
    todayTokens: "今日 Token",
    providers: "Provider",
    balance: "实测余额",
    ledgerDate: "账本日期",
    localFirst: "本地优先，只读，数据留在本机",
    quotaHub: "Quota Hub",
    modelTrend: "模型趋势",
    modelSpend: "按模型消耗",
    noModelTrend:
      "当前筛选下没有匹配的模型活动，切换周期或 Provider 后查看趋势。",
    noSpend: "近 7 天暂无模型用量。",
    refresh: "刷新",
    openConsole: "打开控制台",
    getApiKey: "获取 API Key",
    connected: "已连接",
    addConnection: "添加连接",
    settings: "设置",
    comingSoon: "该界面随 UsageHub v0.8 跨端客户端提供。",
  },
} as const;

export function detectLang(): Lang {
  const saved = localStorage.getItem("usagehub.lang");
  if (saved === "en" || saved === "zh") return saved;
  return navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
}

export function setLang(lang: Lang) {
  localStorage.setItem("usagehub.lang", lang);
}

export type Messages = (typeof messages)["en"];
