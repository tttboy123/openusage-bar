import type { Meta, StoryObj } from "@storybook/react";
import ProviderCard from "../components/ProviderCard";
import { messages } from "../i18n";

const t = messages.en;

const meta: Meta = {
  title: "Provider/ProviderGrid",
  parameters: { layout: "padded" },
};
export default meta;

export const Grid: StoryObj = {
  render: () => (
    <section className="provider-grid" style={{ maxWidth: 720 }}>
      <ProviderCard
        providerId="deepseek"
        displayName="DeepSeek"
        familyId="deepseek"
        source={{ providerId: "deepseek", sourceId: "deepseek", state: "ok", lastSuccessAt: new Date(Date.now() - 3 * 60_000).toISOString() }}
        quick={{ familyId: "deepseek", consoleUrl: "https://platform.deepseek.com", apiKeyUrl: "https://platform.deepseek.com/api_keys", authModes: ["api_key"] }}
        t={t}
      />
      <ProviderCard
        providerId="minimax"
        displayName="MiniMax"
        familyId="minimax"
        source={{ providerId: "minimax", sourceId: "minimax", state: "stale", lastSuccessAt: new Date(Date.now() - 48 * 60 * 60_000).toISOString() }}
        quick={{ familyId: "minimax", consoleUrl: "https://platform.minimaxi.com", apiKeyUrl: "https://platform.minimaxi.com/api-keys", authModes: ["api_key"] }}
        t={t}
      />
      <ProviderCard
        providerId="openai"
        displayName="OpenAI"
        familyId="openai"
        source={{ providerId: "openai", sourceId: "openai", state: "error", errorCode: "unauthorized", lastSuccessAt: null, lastAttemptAt: new Date(Date.now() - 10 * 60_000).toISOString() }}
        quick={{ familyId: "openai", consoleUrl: "https://platform.openai.com", apiKeyUrl: "https://platform.openai.com/api-keys", authModes: ["api_key"] }}
        t={t}
      />
    </section>
  ),
};
