import type { Preview } from "@storybook/react";
import "../src/styles/tokens.css";
import "../src/styles/app.css";

const preview: Preview = {
  parameters: {
    controls: {
      matchers: {
        color: /(background|color)$/i,
        date: /Date$/i,
      },
    },
  },
};

export default preview;
