import { Link } from "react-router-dom";
import { Alert, Spacer, Tag, TableSkeleton, Typography, useDesignSystemTheme } from "@databricks/design-system";
import { api } from "../api";
import { useLoad, when } from "../util";

/** A line that reads as the failure itself, highlighted in the log tail. */
const LOUD = /fatal|error|exception|traceback|failed|killed|oom/i;

/** "Why it failed": the failure followed down to its leaf (a container, a pipeline step's
 *  custom job...), with that leaf's own error, its last log lines and the code that ran. */
export default function WhyFailed({ wf, id, task, start }: { wf: string; id: string; task?: string; start?: string }) {
  const { theme } = useDesignSystemTheme();
  const w = useLoad(() => api.why(wf, id, task), [wf, id, task]);
  if (w.error) return <Alert componentId="why.error" type="warning" message="Couldn't trace this failure" description={w.error} closable={false} />;
  if (!w.data) return <TableSkeleton lines={3} />;
  const { path, error, logs, code } = w.data;
  const redeployed = code.deployed_at && start && Date.parse(code.deployed_at) > Date.parse(start);
  return (
    <div css={{ border: `1px solid ${theme.colors.actionDangerPrimaryBackgroundDefault}`, borderRadius: theme.borders.borderRadiusMd,
                padding: theme.spacing.md, marginBottom: theme.spacing.md }}>
      <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>Why it failed</Typography.Title>
      <div css={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: theme.spacing.xs, margin: `${theme.spacing.sm}px 0` }}>
        {path.map((p, i) => (
          <span key={i} css={{ display: "inline-flex", alignItems: "center", gap: theme.spacing.xs }}>
            {i > 0 && <Typography.Text color="secondary">›</Typography.Text>}
            <Typography.Text color="secondary" size="sm">{p.kind.replace("_", " ")}</Typography.Text>
            {p.link.startsWith("/") ? <Link to={p.link}>{p.label}</Link>
              : <a href={p.link} target="_blank" rel="noreferrer">{p.label}</a>}
          </span>))}
      </div>
      {error && <pre css={{ margin: 0, color: theme.colors.textValidationDanger, whiteSpace: "pre-wrap", wordBreak: "break-word",
                             fontSize: 12, fontFamily: "var(--db-font-family-mono, monospace)" }}>{error}</pre>}
      {logs.length > 0 && (
        <>
          <Spacer size="sm" />
          <Typography.Text color="secondary" size="sm">Its last {logs.length} log lines</Typography.Text>
          <pre css={{ margin: `${theme.spacing.xs}px 0 0`, padding: theme.spacing.sm, maxHeight: 320, overflow: "auto",
                      background: theme.colors.backgroundSecondary, borderRadius: theme.borders.borderRadiusMd,
                      fontSize: 12, lineHeight: "18px", whiteSpace: "pre-wrap", wordBreak: "break-word",
                      fontFamily: "var(--db-font-family-mono, monospace)" }}>
            {logs.map((l, i) => (
              <div key={i} css={LOUD.test(l.text) ? { color: theme.colors.textValidationDanger, fontWeight: 600 } : undefined}>{l.text}</div>))}
          </pre>
        </>
      )}
      {code.commit && (
        <div css={{ marginTop: theme.spacing.sm, display: "flex", alignItems: "center", gap: theme.spacing.sm, flexWrap: "wrap" }}>
          <Typography.Text color="secondary" size="sm">Code</Typography.Text>
          {code.commit_url ? <a href={code.commit_url} target="_blank" rel="noreferrer">{code.branch}@{code.commit.slice(0, 8)}</a>
            : <Typography.Text>{code.branch}@{code.commit.slice(0, 8)}</Typography.Text>}
          {code.dirty && <Tag componentId="why.dirty" color="lemon">with uncommitted changes</Tag>}
          {code.deployed_at && <Typography.Text color="secondary" size="sm">deployed {when(code.deployed_at, true)}{code.deployed_by ? ` by ${code.deployed_by}` : ""}</Typography.Text>}
          {redeployed && <Tag componentId="why.redeployed" color="coral">redeployed since this run</Tag>}
        </div>
      )}
    </div>
  );
}
