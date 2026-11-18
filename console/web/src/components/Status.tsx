import { keyframes } from "@emotion/react";
import {
  CheckCircleIcon, CircleOffIcon, DotsCircleIcon, MinusCircleIcon, SyncIcon, Typography,
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
    case "QUEUED": case "PENDING": case null: case undefined:
      return <DotsCircleIcon css={{ color: theme.colors.textSecondary }} />;
    case "CANCELLED":
      return <MinusCircleIcon css={{ color: theme.colors.textSecondary }} />;
    default:  // skipped, not run: Databricks' Disabled
      return <CircleOffIcon css={{ color: theme.colors.textSecondary }} />;
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

/** Databricks paints a cancelled run or task pink and hatched, apart from a skipped one's grey. */
export const CANCELLED_PAINT = {
  backgroundColor: "#F69CAD",
  backgroundImage: "repeating-linear-gradient(-45deg, rgba(255,255,255,0.35), rgba(255,255,255,0.35) 1px, transparent 1px, transparent 6px)",
} as const;

/** A state's paint as CSS: the hatch for a cancelled one, its solid fill otherwise. */
export function useStatePaint() {
  const fill = useStateFill();
  return (state?: string | null) => state === "CANCELLED" ? CANCELLED_PAINT : { backgroundColor: fill(state) };
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
      case "CANCELLED": return CANCELLED_PAINT.backgroundColor;
      case "ACTIVE": case "RUNNING": return theme.colors.actionPrimaryBackgroundDefault;
      default: return theme.colors.border;
    }
  };
}
