import { useState } from "react";
import { Button, CheckIcon, CopyIcon, Modal, Tabs, Typography, useDesignSystemTheme } from "@databricks/design-system";
import { api } from "../api";
import { useLoad } from "../util";

/** Claude Code on the workspace: the one command that gives it mlops-notebooks-mcp, filled in for
 *  this console, and what to ask it. */
export default function ClaudeSetup({ open, onClose, notebook }: { open: boolean; onClose: () => void; notebook?: string }) {
  const me = useLoad(api.me, []);
  const m = me.data;
  const env: Record<string, string> = m ? {
    MLOPS_PROJECT: m.project, MLOPS_REGION: m.region, MLOPS_WORKSPACE: m.workspace, MLOPS_CONSOLE_URL: window.location.origin,
  } : {};
  const from = m ? `gcpbricks[notebooks-mcp] @ ${m.platform_git}` : "";
  const add = `claude mcp add --scope user mlops-notebooks ${Object.entries(env).map(([k, v]) => `-e ${k}=${v}`).join(" ")} -- uvx --from "${from}" mlops-notebooks-mcp`;
  const json = JSON.stringify({ mcpServers: { "mlops-notebooks": { command: "uvx", args: ["--from", from, "mlops-notebooks-mcp"], env } } }, null, 2);
  const target = notebook ?? (m ? `Users/${m.email}/scratch.ipynb` : "");
  const prompts = notebook
    ? [`Read ${notebook} and explain what it does.`, `In ${notebook}, add a cell that plots the result of the last cell, run it and fix any error.`,
       `Run every cell of ${notebook} on my runtime and summarize the outputs.`]
    : [`Create ${target} that loads yesterday's ml_predictions from BigQuery and plots it, then run it.`,
       "List my notebooks and Colab runtimes.", "Which of my runtimes is running? Start one if none is."];

  return (
    <Modal componentId="claude.setup" title="Use Claude on your notebooks" visible={open} onCancel={onClose} size="wide"
           footer={<Button componentId="claude.close" onClick={onClose}>Done</Button>}>
      <Typography.Paragraph color="secondary">
        Claude Code reads and edits workspace notebooks and runs their cells on your own Colab runtime, in the kernel this
        editor uses. It runs on your machine as you; open the notebook here to watch its edits and outputs land.
      </Typography.Paragraph>
      <Step n={1} title="Sign in to Google as yourself (once)">
        <Command text="gcloud auth application-default login" />
      </Step>
      <Step n={2} title="Add it to Claude Code (needs uv and git access to the platform repo)">
        <Tabs.Root componentId="claude.tabs" defaultValue="cli">
          <Tabs.List><Tabs.Trigger value="cli">Claude Code</Tabs.Trigger><Tabs.Trigger value="json">Other MCP clients (.mcp.json)</Tabs.Trigger></Tabs.List>
          <Tabs.Content value="cli"><Command text={add} /></Tabs.Content>
          <Tabs.Content value="json"><Command text={json} /></Tabs.Content>
        </Tabs.Root>
      </Step>
      <Step n={3} title="Ask Claude, e.g.">
        {prompts.map((p) => <Command key={p} text={p} prose />)}
      </Step>
      <Typography.Text color="secondary" size="sm">
        Cells run on a running runtime of yours (Compute). Starting or making one bills while it runs; Claude Code asks before each
        call unless you allow it. Run <code>/mcp</code> in Claude Code to check the connection.
      </Typography.Text>
    </Modal>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  const { theme } = useDesignSystemTheme();
  return (
    <div css={{ marginBottom: theme.spacing.md }}>
      <Typography.Text bold>{n}. {title}</Typography.Text>
      <div css={{ marginTop: theme.spacing.xs }}>{children}</div>
    </div>
  );
}

function Command({ text, prose }: { text: string; prose?: boolean }) {
  const { theme } = useDesignSystemTheme();
  const [copied, setCopied] = useState(false);
  const copy = () => navigator.clipboard.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
  return (
    <div css={{ display: "flex", alignItems: "flex-start", gap: theme.spacing.xs, marginBottom: theme.spacing.xs,
                background: theme.colors.backgroundSecondary, border: `1px solid ${theme.colors.border}`,
                borderRadius: theme.borders.borderRadiusMd, padding: `${theme.spacing.xs}px ${theme.spacing.sm}px` }}>
      <pre css={{ flex: 1, margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-all", fontSize: 12, paddingTop: 4,
                  fontFamily: prose ? "inherit" : "var(--db-font-family-mono, monospace)" }}>{text}</pre>
      <Button componentId="claude.copy" size="small" icon={copied ? <CheckIcon /> : <CopyIcon />}
              aria-label="Copy" onClick={copy} />
    </div>
  );
}
