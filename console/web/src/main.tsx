import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { ApplyGlobalStyles, DesignSystemProvider, DesignSystemThemeProvider } from "@databricks/design-system";
import "@databricks/design-system/index.css";
import "@databricks/design-system/index-dark.css";
import "@databricks/design-system/fonts/dm-sans.css";
import "@databricks/design-system/fonts/dm-mono.css";
import "@xyflow/react/dist/style.css";
import "./app.css";
import App from "./App";
import { ToastProvider } from "./components/Toast";
import { AppearanceProvider, useAppearanceState } from "./theme";

function Root() {
  const { appearance, setAppearance, isDark } = useAppearanceState();
  return (
    <AppearanceProvider value={{ appearance, setAppearance }}>
      <DesignSystemThemeProvider isDarkMode={isDark}>
        <DesignSystemProvider>
          <ApplyGlobalStyles />
          <BrowserRouter>
            <ToastProvider>
              <App />
            </ToastProvider>
          </BrowserRouter>
        </DesignSystemProvider>
      </DesignSystemThemeProvider>
    </AppearanceProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
