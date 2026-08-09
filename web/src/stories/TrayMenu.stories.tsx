import type { Meta, StoryObj } from "@storybook/react";
import TrayMenu from "../components/TrayMenu";

const meta: Meta<typeof TrayMenu> = {
  component: TrayMenu,
  title: "Native shells/TrayMenu",
  parameters: { layout: "centered" },
};
export default meta;

type Story = StoryObj<typeof TrayMenu>;

const capacities = [
  { provider: "openai-primary", remaining: "50", unit: "USD", ratio: 0.1 },
  { provider: "anthropic-primary", remaining: "180", unit: "USD", ratio: 0.18 },
  { provider: "minimax-primary", remaining: "28", unit: "%", ratio: 0.28 },
  { provider: "gemini_api-primary", remaining: "1,458", unit: "", ratio: 0.97 },
];

export const Default: Story = {
  args: {
    todayTokens: "42,879",
    urgent: { provider: "minimax-primary", remaining: "28", unit: "%" },
    capacities,
  },
};

export const NoUrgent: Story = {
  args: { todayTokens: "12,732", capacities },
};
