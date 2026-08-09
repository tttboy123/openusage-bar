import type { Meta, StoryObj } from "@storybook/react";
import ProviderCard from "../components/ProviderCard";
import { messages } from "../i18n";

const t = messages.en;

const meta: Meta<typeof ProviderCard> = {
  title: "Provider/ProviderCard",
  component: ProviderCard,
  parameters: { layout: "centered" },
};
export default meta;

type Story = StoryObj<typeof ProviderCard>;

const base = {
  providerId: "deepseek-main",
  displayName: "DeepSeek",
  familyId: "deepseek",
  sourceKind: "api_key",
  quick: {
    familyId: "deepseek",
    consoleUrl: "https://platform.deepseek.com",
    apiKeyUrl: "https://platform.deepseek.com/api_keys",
    authModes: ["api_key"],
  },
  t,
};

export const Connected: Story = {
  args: {
    ...base,
    source: {
      providerId: "deepseek-main",
      sourceId: "deepseek-main",
      state: "ok",
      lastSuccessAt: new Date(Date.now() - 2 * 60_000).toISOString(),
    },
  },
};

export const Live: Story = {
  args: {
    ...base,
    source: {
      providerId: "deepseek-main",
      sourceId: "deepseek-main",
      state: "ok",
      lastSuccessAt: new Date(Date.now() - 30_000).toISOString(),
    },
  },
};

export const ConnectedNotLive: Story = {
  args: {
    ...base,
    source: {
      providerId: "deepseek-main",
      sourceId: "deepseek-main",
      state: "ok",
      lastSuccessAt: new Date(Date.now() - 10 * 60_000).toISOString(),
    },
  },
};

export const Stale: Story = {
  args: {
    ...base,
    source: {
      providerId: "deepseek-main",
      sourceId: "deepseek-main",
      state: "stale",
      lastSuccessAt: new Date(Date.now() - 2 * 24 * 60 * 60_000).toISOString(),
    },
  },
};

export const Error: Story = {
  args: {
    ...base,
    source: {
      providerId: "deepseek-main",
      sourceId: "deepseek-main",
      state: "error",
      errorCode: "unauthorized",
      lastSuccessAt: null,
      lastAttemptAt: new Date(Date.now() - 5 * 60_000).toISOString(),
    },
  },
};

export const NoSource: Story = {
  args: { ...base, source: undefined },
};
