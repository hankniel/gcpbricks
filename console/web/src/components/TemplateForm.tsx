import { useEffect, useState } from "react";
import { Alert, FormUI, Input, Modal, SimpleSelect, SimpleSelectOption, Spacer, Switch, Typography, useDesignSystemTheme } from "@databricks/design-system";
import type { RuntimeTemplate } from "../api";

export type TemplateSpec = {
  name: string; description: string; machine: string; accelerator: string; accelerator_count: number; disk_gb: number;
  disk_type: string; idle_minutes: number; internet: boolean; image: string; env: Record<string, string>;
  startup_script: string; startup_script_url: string; startup_behavior: string;
};

const MACHINES = ["e2-standard-4", "e2-standard-8", "e2-highmem-4", "e2-highmem-8", "e2-highmem-16", "n1-standard-4",
  "n1-standard-8", "n1-highmem-8", "n2-standard-8", "n2-highmem-16", "g2-standard-8", "a2-highgpu-1g"];
const GPUS = ["none", "NVIDIA_TESLA_T4", "NVIDIA_L4", "NVIDIA_TESLA_V100", "NVIDIA_TESLA_A100"];
const BEHAVIORS = ["RUN_ONCE", "RUN_EVERY_START", "DOWNLOAD_AND_RUN_EVERY_START"];

export const fromTemplate = (t: RuntimeTemplate, copy: boolean): TemplateSpec => ({
  name: copy ? `${t.name} (copy)` : t.name, description: t.description, machine: t.machine, accelerator: t.accelerator,
  accelerator_count: t.accelerator_count, disk_gb: Number(t.disk_gb) || 100, disk_type: t.disk_type || "pd-standard",
  idle_minutes: t.idle_shutdown ? Math.round(parseInt(t.idle_shutdown, 10) / 60) : 0, internet: t.internet, image: t.image,
  env: t.env, startup_script: t.startup_script, startup_script_url: t.startup_script_url, startup_behavior: t.startup_behavior,
});
export const BLANK: TemplateSpec = { name: "", description: "", machine: "e2-standard-4", accelerator: "", accelerator_count: 0,
  disk_gb: 100, disk_type: "pd-standard", idle_minutes: 180, internet: true, image: "", env: {}, startup_script: "",
  startup_script_url: "", startup_behavior: "RUN_ONCE" };

const envText = (env: Record<string, string>) => Object.entries(env).map(([k, v]) => `${k}=${v}`).join("\n");
const parseEnv = (s: string) => Object.fromEntries(s.split("\n").map((l) => l.trim()).filter((l) => l.includes("="))
  .map((l) => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1).trim()]));

/** A runtime template's settings. Editing, only what Colab lets change after a template is made
 *  (name, env, image, post-startup script) is open; the rest shows, fixed. */
export default function TemplateForm({ open, initial, editing, onCancel, onSubmit }: {
  open: boolean; initial: TemplateSpec; editing: boolean; onCancel: () => void;
  onSubmit: (spec: TemplateSpec) => Promise<void>;
}) {
  const { theme } = useDesignSystemTheme();
  const [s, setS] = useState(initial);
  const [env, setEnv] = useState(envText(initial.env));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (open) { setS(initial); setEnv(envText(initial.env)); setError(null); } }, [open, initial]);
  const set = <K extends keyof TemplateSpec>(k: K, v: TemplateSpec[K]) => setS({ ...s, [k]: v });
  const row = { marginBottom: theme.spacing.sm };
  const two = { display: "grid", gridTemplateColumns: "1fr 1fr", gap: theme.spacing.md };
  const submit = async () => {
    setBusy(true); setError(null);
    try { await onSubmit({ ...s, env: parseEnv(env) }); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <Modal componentId="tpl.form" title={editing ? `Edit ${initial.name}` : "Create a runtime template"} visible={open} size="wide"
           okText={editing ? "Save" : "Create"} cancelText="Cancel" confirmLoading={busy} onCancel={onCancel} onOk={submit}>
      {editing && <><Typography.Paragraph color="secondary">Colab fixes a template's machine, disk, network and idle shutdown when
        it's made: to change those, duplicate it. Runtimes already made from it keep their settings.</Typography.Paragraph></>}
      <div css={two}>
        <div css={row}><FormUI.Label htmlFor="tpl-name">Name</FormUI.Label>
          <Input id="tpl-name" componentId="tpl.name" value={s.name} onChange={(e) => set("name", e.target.value)} /></div>
        <div css={row}><FormUI.Label htmlFor="tpl-desc">Description</FormUI.Label>
          <Input id="tpl-desc" componentId="tpl.desc" value={s.description} disabled={editing} onChange={(e) => set("description", e.target.value)} /></div>
        <div css={row}><FormUI.Label htmlFor="tpl-machine">Machine type</FormUI.Label>
          <SimpleSelect componentId="tpl.machine" id="tpl-machine" value={s.machine} disabled={editing} onChange={(e) => set("machine", e.target.value)}>
            {[...new Set([s.machine, ...MACHINES])].filter(Boolean).map((m) => <SimpleSelectOption key={m} value={m}>{m}</SimpleSelectOption>)}
          </SimpleSelect></div>
        <div css={{ ...row, ...two, gap: theme.spacing.sm }}>
          <div><FormUI.Label htmlFor="tpl-gpu">GPU</FormUI.Label>
            <SimpleSelect componentId="tpl.gpu" id="tpl-gpu" value={s.accelerator || "none"} disabled={editing}
                          onChange={(e) => {
                            const g = e.target.value === "none" ? "" : e.target.value;
                            setS({ ...s, accelerator: g, accelerator_count: g ? Math.max(1, s.accelerator_count) : 0 });
                          }}>
              {GPUS.map((g) => <SimpleSelectOption key={g} value={g}>{g === "none" ? "None" : g}</SimpleSelectOption>)}
            </SimpleSelect></div>
          <div><FormUI.Label htmlFor="tpl-gpus">GPUs</FormUI.Label>
            <Input id="tpl-gpus" componentId="tpl.gpus" type="number" min={0} value={String(s.accelerator_count)} disabled={editing || !s.accelerator}
                   onChange={(e) => set("accelerator_count", Number(e.target.value))} /></div>
        </div>
        <div css={{ ...row, ...two, gap: theme.spacing.sm }}>
          <div><FormUI.Label htmlFor="tpl-disk">Data disk (GB)</FormUI.Label>
            <Input id="tpl-disk" componentId="tpl.disk" type="number" min={10} value={String(s.disk_gb)} disabled={editing}
                   onChange={(e) => set("disk_gb", Number(e.target.value))} /></div>
          <div><FormUI.Label htmlFor="tpl-disktype">Disk type</FormUI.Label>
            <SimpleSelect componentId="tpl.disktype" id="tpl-disktype" value={s.disk_type} disabled={editing} onChange={(e) => set("disk_type", e.target.value)}>
              {["pd-standard", "pd-balanced", "pd-ssd"].map((d) => <SimpleSelectOption key={d} value={d}>{d}</SimpleSelectOption>)}
            </SimpleSelect></div>
        </div>
        <div css={row}><FormUI.Label htmlFor="tpl-idle">Idle shutdown (minutes; 0 = never)</FormUI.Label>
          <Input id="tpl-idle" componentId="tpl.idle" type="number" min={0} max={1440} value={String(s.idle_minutes)} disabled={editing}
                 onChange={(e) => set("idle_minutes", Number(e.target.value))} /></div>
        <div css={{ ...row, display: "flex", alignItems: "center", gap: theme.spacing.sm, paddingTop: 22 }}>
          <Switch componentId="tpl.internet" checked={s.internet} disabled={editing} onChange={(v) => set("internet", v)} />
          <Typography.Text>Internet access (pip install, public APIs)</Typography.Text></div>
        <div css={row}><FormUI.Label htmlFor="tpl-image">Colab image release (empty: latest)</FormUI.Label>
          <Input id="tpl-image" componentId="tpl.image" value={s.image} placeholder="e.g. py311" onChange={(e) => set("image", e.target.value)} /></div>
        <div css={row}><FormUI.Label htmlFor="tpl-behavior">Post-startup script runs</FormUI.Label>
          <SimpleSelect componentId="tpl.behavior" id="tpl-behavior" value={s.startup_behavior || "RUN_ONCE"} onChange={(e) => set("startup_behavior", e.target.value)}>
            {BEHAVIORS.map((b) => <SimpleSelectOption key={b} value={b}>{b.toLowerCase().replace(/_/g, " ")}</SimpleSelectOption>)}
          </SimpleSelect></div>
      </div>
      <div css={row}><FormUI.Label htmlFor="tpl-script">Post-startup script (bash; e.g. pip install your locked requirements)</FormUI.Label>
        <Input.TextArea id="tpl-script" componentId="tpl.script" rows={4} value={s.startup_script} css={{ fontFamily: "monospace" }}
                        onChange={(e) => set("startup_script", e.target.value)} /></div>
      <div css={row}><FormUI.Label htmlFor="tpl-scripturl">or a script in GCS</FormUI.Label>
        <Input id="tpl-scripturl" componentId="tpl.scripturl" value={s.startup_script_url} placeholder="gs://bucket/startup.sh"
               onChange={(e) => set("startup_script_url", e.target.value)} /></div>
      <div css={row}><FormUI.Label htmlFor="tpl-env">Environment variables (NAME=value, one per line)</FormUI.Label>
        <Input.TextArea id="tpl-env" componentId="tpl.env" rows={3} value={env} css={{ fontFamily: "monospace" }} onChange={(e) => setEnv(e.target.value)} /></div>
      {error && <><Spacer size="sm" /><Alert componentId="tpl.error" type="error" message={error} closable={false} /></>}
    </Modal>
  );
}
