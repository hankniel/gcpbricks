import { useEffect, useState } from "react";
import {
  Alert, Button, Drawer, FormUI, Input, Radio, SimpleSelect, SimpleSelectOption, Spacer, Tag, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { notebooksApi, type NotebookRuntime, type RuntimeTemplate } from "../api";
import { secondsText } from "../util";

/** An env value whose name says it's a secret shows masked: templates are readable by everyone. */
const SECRET = /TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIAL/i;
const word = (s: string) => (s ? s.charAt(0) + s.slice(1).toLowerCase().replace(/_/g, " ") : "-");
const stamp = () => {
  const d = new Date(), p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
};

/** Colab's "Connect to runtime" panel in the console: connect the notebook to a runtime of yours
 *  (starting it if it's stopped), or create one from a template, its settings laid out first. */
export default function RuntimeDrawer({ open, onClose, user, runtimes, templates, current, onConnect }: {
  open: boolean; onClose: () => void; user: string; runtimes: NotebookRuntime[]; templates: RuntimeTemplate[];
  current?: string; onConnect: (runtime: { id?: string; name: string }) => void;
}) {
  const { theme } = useDesignSystemTheme();
  const [mode, setMode] = useState<"existing" | "new">("existing");
  const [picked, setPicked] = useState("");
  const [template, setTemplate] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setMode(runtimes.length ? "existing" : "new");
    setPicked(current ?? runtimes.find((r) => r.state === "RUNNING")?.id ?? runtimes[0]?.id ?? "");
    setTemplate(templates.find((t) => t.default)?.id ?? templates[0]?.id ?? "");
    setName(`${user.split("@")[0]}-${stamp()}`);
    setError(null);
    // Each opening starts from what's there; a runtime list refreshing meanwhile keeps the choice.
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps
  const rt = runtimes.find((r) => r.id === picked);
  const tpl = templates.find((t) => t.id === template);

  const submit = async () => {
    setBusy(true); setError(null);
    try {
      if (mode === "existing") {
        if (!rt) return;
        if (rt.state !== "RUNNING" && !rt.state.startsWith("BEING_")) await notebooksApi.runtimeAction(rt.id, "start");
        onConnect({ id: rt.id, name: rt.name });
      } else {
        const made = await notebooksApi.createRuntime(template, name);
        onConnect({ name: made.name });
      }
      onClose();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const ok = mode === "existing"
    ? rt?.state === "RUNNING" ? "Connect" : "Start and connect"
    : "Create and connect";

  return (
    <Drawer.Root open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      <Drawer.Content componentId="nb.connect" title="Connect to a runtime" width={560}
        footer={<div css={{ display: "flex", justifyContent: "flex-end", gap: theme.spacing.sm }}>
          <Button componentId="nb.connect.cancel" onClick={onClose}>Cancel</Button>
          <Button componentId="nb.connect.ok" type="primary" loading={busy} onClick={submit}
                  disabled={mode === "existing" ? !rt : !template || !name.trim()}>{ok}</Button>
        </div>}>
        <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>Select a runtime option</Typography.Title>
        <Spacer size="xs" />
        <Radio.Group componentId="nb.connect.mode" name="nb-connect-mode" layout="horizontal" value={mode}
                     onChange={(e) => setMode(e.target.value as "existing" | "new")}>
          <Radio value="existing" disabled={!runtimes.length}>Connect to an existing runtime</Radio>
          <Radio value="new">Create new runtime</Radio>
        </Radio.Group>
        <Spacer size="lg" />
        {error && <><Alert componentId="nb.connect.error" type="error" message={error} closable={false} /><Spacer size="sm" /></>}

        {mode === "existing" && (
          <>
            <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>Your runtimes</Typography.Title>
            <Spacer size="xs" />
            <Radio.Group componentId="nb.connect.runtime" name="nb-connect-runtime" value={picked} onChange={(e) => setPicked(e.target.value)}>
              {runtimes.map((r) => (
                <Radio key={r.id} value={r.id}>
                  <span css={{ display: "inline-flex", alignItems: "center", gap: theme.spacing.sm, flexWrap: "wrap" }}>
                    <Typography.Text>{r.name}</Typography.Text>
                    <Tag componentId="nb.connect.state" color={r.state === "RUNNING" ? "lime" : r.state.startsWith("BEING_") ? "lemon" : "charcoal"}>
                      {word(r.state)}</Tag>
                    {r.id === current && <Tag componentId="nb.connect.current" color="indigo">this notebook's</Tag>}
                    <Typography.Text color="secondary" size="sm">
                      {[templates.find((t) => t.id === r.template)?.name, r.machine].filter(Boolean).join(" · ")}</Typography.Text>
                  </span>
                </Radio>))}
            </Radio.Group>
            {rt && rt.state !== "RUNNING" && (
              <><Spacer size="sm" /><Typography.Hint>{rt.state.startsWith("BEING_")
                ? `${rt.name} is ${word(rt.state).toLowerCase()}: cells run once it's up.`
                : `${rt.name} is ${word(rt.state).toLowerCase()}: connecting starts it, and it bills while it runs.`}</Typography.Hint></>)}
          </>
        )}

        {mode === "new" && (
          <>
            <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>Create a new runtime</Typography.Title>
            <Spacer size="sm" />
            <FormUI.Label htmlFor="nb-connect-template">Runtime template</FormUI.Label>
            <SimpleSelect componentId="nb.connect.template" id="nb-connect-template" value={template} css={{ width: "100%" }}
                          onChange={(e) => setTemplate(e.target.value)}>
              {templates.map((t) => <SimpleSelectOption key={t.id} value={t.id}>{t.name}{t.default ? " (default)" : ""}</SimpleSelectOption>)}
            </SimpleSelect>
            <Spacer size="sm" />
            <FormUI.Label htmlFor="nb-connect-name">Runtime name</FormUI.Label>
            <Input id="nb-connect-name" componentId="nb.connect.name" value={name} onChange={(e) => setName(e.target.value)} />
            <Spacer size="sm" />
            <FormUI.Label htmlFor="nb-connect-user">Runtime user</FormUI.Label>
            <Input id="nb-connect-user" componentId="nb.connect.user" value={user} disabled />
            <FormUI.Hint>A runtime runs code as its user, and only its user can connect to it.</FormUI.Hint>
            {tpl && (
              <>
                <Spacer size="lg" />
                <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>Template details</Typography.Title>
                <Spacer size="xs" />
                <dl css={{ display: "grid", gridTemplateColumns: "140px minmax(0, 1fr)", margin: 0,
                           "& > *": { padding: `${theme.spacing.xs}px 0`, borderBottom: `1px solid ${theme.colors.border}`, margin: 0 },
                           "& dt": { color: theme.colors.textSecondary } }}>
                  {[["Name", tpl.name], ["Description", tpl.description || "-"], ["Machine type", tpl.machine || "-"],
                    ["GPU", tpl.accelerator ? `${tpl.accelerator_count} × ${tpl.accelerator}` : "-"],
                    ["Data disk", tpl.disk_gb ? `${tpl.disk_gb} GB${tpl.disk_type ? ` (${tpl.disk_type})` : ""}` : "-"],
                    ["Idle shutdown", tpl.idle_shutdown ? secondsText(parseInt(tpl.idle_shutdown, 10)) : "Never"],
                    ["Internet access", tpl.internet ? "Enabled" : "Disabled"],
                    ["Environment", tpl.image || "Latest"],
                    ["Post-startup script", tpl.startup_script ? `inline, ${tpl.startup_behavior.toLowerCase().replace(/_/g, " ")}`
                      : tpl.startup_script_url || "-"],
                  ].map(([k, v]) => [<dt key={`${k}t`}>{k}</dt>, <dd key={`${k}d`} css={{ wordBreak: "break-word" }}>{v}</dd>])}
                  <dt>Env variables</dt>
                  <dd css={{ fontFamily: "monospace", fontSize: 12, wordBreak: "break-all" }}>
                    {Object.keys(tpl.env).length ? Object.entries(tpl.env).map(([k, v]) => (
                      <div key={k}>{k}={SECRET.test(k) ? "••••••••" : v}</div>)) : "-"}
                  </dd>
                </dl>
                <Spacer size="sm" />
                <Typography.Hint>A runtime bills while it runs and stops after the idle shutdown above.</Typography.Hint>
              </>
            )}
          </>
        )}
      </Drawer.Content>
    </Drawer.Root>
  );
}
