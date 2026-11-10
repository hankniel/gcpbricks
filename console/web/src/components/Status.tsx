import { keyframes } from "@emotion/react";
import {
  CheckCircleIcon, DotsCircleIcon, MinusCircleIcon, StopCircleIcon, SyncIcon, Typography,
  XCircleIcon, useDesignSystemTheme,
} from "@databricks/design-system";
import { label } from "../util";

const spin = keyframes({ to: { transform: "rotate(360deg)" } });

/** Databricks' outline status icons: green check, red cross, green arrows turning while it runs. */
export function StatusIcon({ state }: { state: string | null | undefined }) {
  const { theme } = useDesignSystemTheme();
  switch (state) {
    case "SUCCEEDED": case "SUCCESS": case "COMPLETE":
      return <CheckCircleIcon css={{ color: theme.colors.textValidationSuccess }} />;
    case "SUCCEEDED_WITH_FAILURES":
      return <CheckCircleIcon css={{ color: theme.colors.textValidationWarning }} />;
    case "REUSED":
      return <CheckCircleIcon css={{ color: theme.colors.textValidationSuccess }} />;
    case "FAILED": case "UPSTREAM_FAILED":
      return <XCircleIcon css={{ color: theme.colors.textValidationDanger }} />;
    case "ACTIVE": case "RUNNING":
      return <SyncIcon css={{ color: theme.colors.textValidationSuccess, display: "inline-flex",
                              animation: `${spin} 1.6s linear infinite`,
                              "@media (prefers-reduced-motion: reduce)": { animation: "none" } }} />;
    case "QUEUED": case "PENDING":
      return <DotsCircleIcon css={{ color: theme.colors.textSecondary }} />;
    case "CANCELLED":
      return <StopCircleIcon css={{ color: theme.colors.textSecondary }} />;
    default:
      return <MinusCircleIcon css={{ color: theme.colors.textSecondary }} />;
  }
}

export function Status({ state }: { state: string | null | undefined }) {
  const { theme } = useDesignSystemTheme();
  return (
    // Top-aligned: on the baseline, an inline-flex icon and label grow a table row by its descender.
    <span css={{ display: "inline-flex", alignItems: "center", gap: theme.spacing.xs, whiteSpace: "nowrap", verticalAlign: "top" }}>
      <StatusIcon state={state} />
      <Typography.Text>{label[state ?? ""] ?? state ?? "Pending"}</Typography.Text>
    </span>
  );
}

/** The solid fill Databricks uses for a run's bar and a task's square in the matrix. */
export function useStateFill() {
  const { theme } = useDesignSystemTheme();
  return (state?: string | null) => {
    switch (state) {
      case "SUCCEEDED": case "SUCCESS": case "REUSED": return theme.colors.textValidationSuccess;
      case "SUCCEEDED_WITH_FAILURES": return theme.colors.textValidationWarning;
      case "FAILED": return theme.colors.textValidationDanger;
      case "UPSTREAM_FAILED": return theme.colors.backgroundDanger;
      case "ACTIVE": case "RUNNING": return theme.colors.actionPrimaryBackgroundDefault;
      default: return theme.colors.border;
    }
  };
}
