import type { Meta, StoryObj } from "@storybook/react";
import MenuBarPopover from "../components/MenuBarPopover";

const meta: Meta<typeof MenuBarPopover> = {
  component: MenuBarPopover,
  title: "Native shells/MenuBarPopover",
  parameters: { layout: "centered" },
};
export default meta;

type Story = StoryObj<typeof MenuBarPopover>;

const groups = [
  {
    id: "openai-primary",
    provider: "OpenAI",
    familyId: "openai",
    window: "tier_1",
    capacity: "50 left",
    status: "warning" as const,
    secondary: [],
  },
  {
    id: "anthropic-primary",
    provider: "Anthropic",
    familyId: "anthropic",
    window: "build_tier",
    capacity: "180 USD",
    status: "warning" as const,
    secondary: [],
  },
  {
    id: "minimax-primary",
    provider: "MiniMax",
    familyId: "minimax",
    window: "five_hour",
    capacity: "28%",
    status: "critical" as const,
    secondary: [],
  },
  {
    id: "gemini-primary",
    provider: "Gemini",
    familyId: "gemini_api",
    window: "daily_free",
    capacity: "1,458 left",
    status: "ok" as const,
    secondary: [
      { id: "gemini-secondary", provider: "Gemini", familyId: "gemini_api", window: "pro_tier", capacity: "OK", status: "ok" as const, secondary: [] },
    ],
  },
];

export const Default: Story = {
  args: {
    updatedAt: "Updated just now",
    todayTokens: "42,879",
    coverage: "5 providers · 8 models",
    groups,
  },
};

export const Empty: Story = {
  args: {
    updatedAt: "Updated 2 min ago",
    todayTokens: "—",
    groups: [],
  },
};
