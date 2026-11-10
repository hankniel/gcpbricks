import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  Alert, Button, CheckCircleIcon, DangerModal, DropdownMenu, Empty, FormUI, Input, Modal, OverflowIcon,
  SimpleSelect, SimpleSelectOption, SparkleDoubleIcon, Spacer, TableCell, TableRow, TableSkeleton, Tabs, Tag,
  Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../components/Table";
import { api, notebooksApi, type RuntimeTemplate } from "../api";
import ClaudeSetup from "../components/ClaudeSetup";
import PageTitle from "../components/PageTitle";
import TemplateForm, { BLANK, fromTemplate, type TemplateSpec } from "../components/TemplateForm";
import { StatusIcon } from "../components/Status";
import { ago, secondsText, useLoad, when } from "../util";

/** A runtime state's icon where one fits; every state reads as its own word beside it. */
const ICON: Record<string, string> = { RUNNING: "RUNNING", BEING_STARTED: "QUEUED", BEING_STOPPED: "QUEUED",
  BEING_UPGRADED: "QUEUED", ERROR: "FAILED", INVALID: "FAILED" };
const word = (s: string) => (s ? s.charAt(0) + s.slice(1).toLowerCase().replace(/_/g, " ") : "-");

const TABS = ["notebooks", "runtimes", "templates"];

/** Notebooks on Colab Enterprise in one place: what running one takes (a runtime of yours,
 *  notebook access, Claude) and your notebooks; the runtimes everyone runs; the templates
 *  they're made from. ?tab= picks the tab, ?create=runtime opens Create runtime. */
export default function NotebooksPage() {
  const { theme } = useDesignSystemTheme();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const tab = TABS.includes(params.get("tab") ?? "") ? params.get("tab")! : "notebooks";
  const me = useLoad(api.me, []);
  const runtimes = useLoad(notebooksApi.runtimes, [], 15000);
  const templates = useLoad(notebooksApi.templates, []);
  const access = useLoad(notebooksApi.access, []);
  const home = me.data ? `Users/${me.data.email}` : "";
  const mine = useLoad(() => (home ? notebooksApi.folder(home) : Promise.resolve(null)), [home]);
  const [ask, setAsk] = useState(params.get("create") === "runtime");
  const [claude, setClaude] = useState(false);
  const [deleting, setDeleting] = useState<{ kind: "runtime" | "template"; id: string; name: string } | null>(null);
  const [newNb, setNewNb] = useState<{ name: string; where: string } | null>(null);
  const [template, setTemplate] = useState("");
  const [msg, setMsg] = useState<{ text: string; error?: boolean } | null>(null);
  const [form, setForm] = useState<{ initial: TemplateSpec; edit?: RuntimeTemplate } | null>(null);
  const admin = !!me.data?.admin;
  const saveTemplate = async (spec: TemplateSpec) => {
    if (form?.edit) {
      const t = form.edit;
      const changed: Record<string, unknown> = {};
      if (spec.name !== t.name) changed.name = spec.name;
      if (JSON.stringify(spec.env) !== JSON.stringify(t.env)) changed.env = spec.env;
      if (spec.image !== t.image) changed.image = spec.image;
      if (spec.startup_script !== t.startup_script) changed.startup_script = spec.startup_script;
      if (spec.startup_script_url !== t.startup_script_url) changed.startup_script_url = spec.startup_script_url;
      if (spec.startup_behavior !== t.startup_behavior && (spec.startup_script || spec.startup_script_url)) changed.startup_behavior = spec.startup_behavior;
      if (!Object.keys(changed).length) { setForm(null); return; }
      await notebooksApi.updateTemplate(t.id, { ...changed, etag: t.etag });
      setMsg({ text: `Saved ${spec.name}.` });
    } else {
      await notebooksApi.createTemplate(spec);
      setMsg({ text: `Creating ${spec.name}: it appears in a few seconds.` });
    }
    setForm(null); templates.reload();
  };
  const act = async (what: () => Promise<unknown>, done: string) => {
    try { await what(); setMsg({ text: done }); runtimes.reload(); } catch (e) { setMsg({ text: (e as Error).message, error: true }); }
  };
  const may = (user: string) => !!me.data && (me.data.admin || me.data.email === user);
  const chosen = template || templates.data?.find((t) => t.default)?.id || templates.data?.[0]?.id || "";
  const myRuntimes = (runtimes.data ?? []).filter((r) => r.user === me.data?.email);
  const running = myRuntimes.find((r) => r.state === "RUNNING");
  const createNotebook = async () => {
    const name = newNb!.name.trim().replace(/^\/+|\/+$/g, "");
    if (!name) return;
    try {
      const r = await notebooksApi.create(`${newNb!.where}/${name}`);
      nav(`/workspace/notebook?path=${encodeURIComponent(r.path)}`);
    } catch (e) { setMsg({ text: (e as Error).message, error: true }); setNewNb(null); }
  };
  const step = (done: boolean, title: string, detail: React.ReactNode, action?: React.ReactNode) => (
    <div css={{ display: "grid", gridTemplateColumns: "24px minmax(0, 1fr) auto", gap: theme.spacing.sm, alignItems: "center",
                padding: `${theme.spacing.sm}px 0`, borderBottom: `1px solid ${theme.colors.border}` }}>
      {/* The mark sits on the title's line, not midway down a wrapped description. */}
      <span css={{ alignSelf: "start", height: 20, display: "flex", alignItems: "center", justifyContent: "center" }}>
        {done ? <CheckCircleIcon css={{ color: theme.colors.textValidationSuccess }} />
          : <span css={{ width: 14, height: 14, borderRadius: "50%", border: `2px solid ${theme.colors.actionDefaultBorderDefault}` }} />}
      </span>
      <div><Typography.Text bold>{title}</Typography.Text><div><Typography.Text color="secondary" size="sm">{detail}</Typography.Text></div></div>
      <div>{action}</div>
    </div>
  );
  return (
    <>
      <PageTitle title="Notebooks" actions={
        <div css={{ display: "flex", gap: theme.spacing.sm }}>
          <Button componentId="nbs.claude" icon={<SparkleDoubleIcon />} onClick={() => setClaude(true)}>Use with Claude</Button>
          <Button componentId="compute.new" onClick={() => setAsk(true)}>Create runtime</Button>
          <Button componentId="nbs.new" type="primary" disabled={!home} onClick={() => setNewNb({ name: "", where: home })}>New notebook</Button>
        </div>} />
      <Spacer size="md" />
      {msg && <><Alert componentId="compute.msg" type={msg.error ? "error" : "info"} message={msg.text} onClose={() => setMsg(null)} /><Spacer size="sm" /></>}
      <Tabs.Root componentId="compute.tabs" value={tab} onValueChange={(v) => setParams(v === "notebooks" ? {} : { tab: v })}>
        <Tabs.List>
          <Tabs.Trigger value="notebooks">Notebooks</Tabs.Trigger>
          <Tabs.Trigger value="runtimes">Runtimes</Tabs.Trigger>
          <Tabs.Trigger value="templates">Templates</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="notebooks">
          <Typography.Title level={4}>Run notebooks</Typography.Title>
          <div css={{ maxWidth: 900 }}>
            {step(!!running, "A runtime of yours",
              running ? `${running.name} is running.` : myRuntimes.length
                ? `${myRuntimes[0].name} is ${word(myRuntimes[0].state).toLowerCase()}: start it to run cells.`
                : "Cells run on a Colab runtime of your own, made from a template. It bills while it runs.",
              running ? null : myRuntimes.length
                ? <Button componentId="nbs.step.start" onClick={() => act(() => notebooksApi.runtimeAction(myRuntimes[0].id, "start"), `Starting ${myRuntimes[0].name}.`)}>Start</Button>
                : <Button componentId="nbs.step.create" onClick={() => setAsk(true)}>Create runtime</Button>)}
            {step(!!access.data?.allowed, "Notebook access",
              !access.data ? "" : !access.data.configured
                ? "Not set up on this console yet: an admin adds its Google OAuth client (NOTEBOOK_OAUTH_CLIENT_ID / _SECRET CI variables). Editing and saving work meanwhile."
                : access.data.allowed ? "Allowed: the console runs cells as you for the next hour."
                  : "Your runtime runs code as you, so the console needs your Google sign-in (once an hour).",
              access.data?.configured && !access.data.allowed
                ? <Button componentId="nbs.step.allow" onClick={() => { window.location.href = notebooksApi.allowUrl("/notebooks"); }}>Allow notebook access</Button>
                : null)}
            {step(false, "Claude (optional)", "Let Claude Code read, edit and run your notebooks from your terminal.",
              <Button componentId="nbs.step.claude" icon={<SparkleDoubleIcon />} onClick={() => setClaude(true)}>Set up</Button>)}
          </div>
          <Spacer size="lg" />
          <div css={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>Your notebooks</Typography.Title>
            {home && <Typography.Link componentId="nbs.browse" onClick={() => nav(`/workspace?path=${encodeURIComponent(home)}`)}>Browse the workspace</Typography.Link>}
          </div>
          <Spacer size="sm" />
          {!mine.data && !mine.error && <TableSkeleton lines={3} />}
          {mine.data && (
            <Table scrollable empty={mine.data.notebooks.length ? undefined
              : <Empty title="No notebooks yet" description="Create one, or ask Claude to." />}>
              <TableRow isHeader>
                <TableHeader componentId="nbs.h.name">Name</TableHeader>
                <TableHeader componentId="nbs.h.updated">Last modified</TableHeader>
              </TableRow>
              {[...mine.data.notebooks].sort((a, b) => (b.updated ?? "").localeCompare(a.updated ?? "")).map((n) => (
                <TableRow key={n.path}>
                  <TableCell><Link to={`/workspace/notebook?path=${encodeURIComponent(n.path)}`}>{n.name.replace(/\.ipynb$/, "")}</Link></TableCell>
                  <TableCell><span title={when(n.updated, true)}>{ago(n.updated)}</span></TableCell>
                </TableRow>))}
            </Table>
          )}
        </Tabs.Content>
        <Tabs.Content value="runtimes">
          <Typography.Paragraph color="secondary">Colab Enterprise runtimes: a notebook's cells run on its owner's. A runtime bills while it
            runs and shuts down when idle as its template says.</Typography.Paragraph>
          {runtimes.error && <Alert componentId="compute.rt.error" type="error" message="Couldn't list runtimes" description={runtimes.error} closable={false} />}
          {!runtimes.data && !runtimes.error && <TableSkeleton lines={4} />}
          {runtimes.data && (
            <Table scrollable empty={runtimes.data.length ? undefined : <Empty title="No runtimes" description="Create one to run notebooks." />}>
              <TableRow isHeader>
                <TableHeader componentId="compute.h.name">Name</TableHeader>
                <TableHeader componentId="compute.h.state">State</TableHeader>
                <TableHeader componentId="compute.h.user">Owner</TableHeader>
                <TableHeader componentId="compute.h.template">Template</TableHeader>
                <TableHeader componentId="compute.h.machine">Machine</TableHeader>
                <TableHeader componentId="compute.h.updated">Last changed</TableHeader>
                <TableHeader componentId="compute.h.actions" css={{ flex: "0 0 48px" }}> </TableHeader>
              </TableRow>
              {runtimes.data.map((r) => (
                <TableRow key={r.id}>
                  <TableCell>{r.name}{r.user === me.data?.email && <> <Tag componentId="compute.mine" color="indigo">yours</Tag></>}</TableCell>
                  <TableCell>{ICON[r.state] ? <StatusIcon state={ICON[r.state]} /> : null}<Typography.Text color={r.state === "RUNNING" ? undefined : "secondary"}> {word(r.state)}</Typography.Text></TableCell>
                  <TableCell>{r.user}</TableCell>
                  <TableCell>{templates.data?.find((t) => t.id === r.template)?.name ?? r.template}</TableCell>
                  <TableCell>{r.machine || "-"}</TableCell>
                  <TableCell><span title={when(r.updated, true)}>{ago(r.updated)}</span></TableCell>
                  <TableCell css={{ flex: "0 0 48px" }}>
                    {may(r.user) && (
                      <DropdownMenu.Root>
                        <DropdownMenu.Trigger asChild><Button componentId="compute.rt.more" size="small" icon={<OverflowIcon />} aria-label={`Actions for ${r.name}`} /></DropdownMenu.Trigger>
                        <DropdownMenu.Content align="end">
                          <DropdownMenu.Item componentId="compute.rt.start" disabled={r.state === "RUNNING"} onClick={() => act(() => notebooksApi.runtimeAction(r.id, "start"), `Starting ${r.name}.`)}>Start</DropdownMenu.Item>
                          <DropdownMenu.Item componentId="compute.rt.stop" disabled={r.state !== "RUNNING"} onClick={() => act(() => notebooksApi.runtimeAction(r.id, "stop"), `Stopping ${r.name}.`)}>Stop</DropdownMenu.Item>
                          <DropdownMenu.Separator />
                          <DropdownMenu.Item componentId="compute.rt.delete" onClick={() => setDeleting({ kind: "runtime", id: r.id, name: r.name })}>Delete</DropdownMenu.Item>
                        </DropdownMenu.Content>
                      </DropdownMenu.Root>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </Table>
          )}
        </Tabs.Content>
        <Tabs.Content value="templates">
          <div css={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <Typography.Text color="secondary">What a runtime is made from: its machine, disk, idle shutdown and software.
              {admin ? "" : " Only the console's admins change templates."}</Typography.Text>
            {admin && <Button componentId="compute.t.new" onClick={() => setForm({ initial: BLANK })}>Create template</Button>}
          </div>
          <Spacer size="sm" />
          {!templates.data && !templates.error && <TableSkeleton lines={3} />}
          {templates.data && (
            <Table scrollable>
              <TableRow isHeader>
                <TableHeader componentId="compute.t.name">Template</TableHeader>
                <TableHeader componentId="compute.t.machine">Machine</TableHeader>
                <TableHeader componentId="compute.t.gpu">Accelerator</TableHeader>
                <TableHeader componentId="compute.t.disk">Disk</TableHeader>
                <TableHeader componentId="compute.t.idle">Idle shutdown</TableHeader>
                <TableHeader componentId="compute.t.sw">Software</TableHeader>
                <TableHeader componentId="compute.t.actions" css={{ flex: "0 0 48px" }}> </TableHeader>
              </TableRow>
              {templates.data.map((t) => (
                <TableRow key={t.id}>
                  <TableCell>{t.name}{t.default && <> <Tag componentId="compute.t.default">default</Tag></>}</TableCell>
                  <TableCell>{t.machine || "-"}</TableCell>
                  <TableCell>{t.accelerator || "-"}</TableCell>
                  <TableCell>{t.disk_gb ? `${t.disk_gb} GB` : "-"}</TableCell>
                  <TableCell>{t.idle_shutdown ? secondsText(parseInt(t.idle_shutdown, 10)) : "Never"}</TableCell>
                  <TableCell><Typography.Text color="secondary" ellipsis>{[t.image && `image ${t.image}`,
                    (t.startup_script || t.startup_script_url) && "startup script", Object.keys(t.env).length && `${Object.keys(t.env).length} env`]
                    .filter(Boolean).join(" · ") || "-"}</Typography.Text></TableCell>
                  <TableCell css={{ flex: "0 0 48px" }}>
                    {admin && (
                      <DropdownMenu.Root>
                        <DropdownMenu.Trigger asChild><Button componentId="compute.t.more" size="small" icon={<OverflowIcon />} aria-label={`Actions for ${t.name}`} /></DropdownMenu.Trigger>
                        <DropdownMenu.Content align="end">
                          <DropdownMenu.Item componentId="compute.t.edit" onClick={() => setForm({ initial: fromTemplate(t, false), edit: t })}>Edit</DropdownMenu.Item>
                          <DropdownMenu.Item componentId="compute.t.dup" onClick={() => setForm({ initial: fromTemplate(t, true) })}>Duplicate</DropdownMenu.Item>
                          <DropdownMenu.Separator />
                          <DropdownMenu.Item componentId="compute.t.delete" onClick={() => setDeleting({ kind: "template", id: t.id, name: t.name })}>Delete</DropdownMenu.Item>
                        </DropdownMenu.Content>
                      </DropdownMenu.Root>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </Table>
          )}
        </Tabs.Content>
      </Tabs.Root>
      <TemplateForm open={!!form} initial={form?.initial ?? BLANK} editing={!!form?.edit} onCancel={() => setForm(null)} onSubmit={saveTemplate} />
      <ClaudeSetup open={claude} onClose={() => setClaude(false)} />
      <DangerModal componentId="nbs.delete" visible={!!deleting} title={`Delete ${deleting?.kind} ${deleting?.name ?? ""}?`}
                   okText={`Delete ${deleting?.kind ?? ""}`} cancelText="Cancel" onCancel={() => setDeleting(null)}
                   onOk={() => {
                     const d = deleting!;
                     setDeleting(null);
                     if (d.kind === "runtime") act(() => notebooksApi.deleteRuntime(d.id), `Deleting ${d.name}.`);
                     else notebooksApi.deleteTemplate(d.id).then(() => { setMsg({ text: `Deleted ${d.name}.` }); templates.reload(); },
                                                                  (e: Error) => setMsg({ text: e.message, error: true }));
                   }}>
        {deleting?.kind === "runtime"
          ? "The runtime, its disk and anything saved on it are gone; notebooks connected to it lose their kernel. Notebooks themselves stay in the workspace."
          : "Runtimes already made from it keep running; no new runtime can be made from it. This can't be undone."}
      </DangerModal>
      <Modal componentId="nbs.new.modal" title="New notebook" visible={!!newNb} okText="Create" cancelText="Cancel"
             onCancel={() => setNewNb(null)} onOk={createNotebook}>
        <FormUI.Label htmlFor="nbs-name">Name</FormUI.Label>
        <Input id="nbs-name" componentId="nbs.new.name" autoFocus value={newNb?.name ?? ""} placeholder="analysis"
               onChange={(e) => setNewNb({ ...newNb!, name: e.target.value })} onPressEnter={createNotebook} />
        <Spacer size="sm" />
        <FormUI.Label htmlFor="nbs-where">In</FormUI.Label>
        <SimpleSelect componentId="nbs.new.where" id="nbs-where" value={newNb?.where ?? home} onChange={(e) => setNewNb({ ...newNb!, where: e.target.value })}>
          <SimpleSelectOption value={home}>My folder ({home})</SimpleSelectOption>
          <SimpleSelectOption value="Shared">Shared</SimpleSelectOption>
        </SimpleSelect>
      </Modal>
      <Modal componentId="compute.new.modal" title="Create a notebook runtime" visible={ask} okText="Create" cancelText="Cancel"
             onCancel={() => setAsk(false)} onOk={() => { setAsk(false); act(() => notebooksApi.createRuntime(chosen), "Creating your runtime: it starts in a few minutes."); }}>
        <Typography.Paragraph>A runtime of yours, from a template. It bills while it runs, and stops when idle as the template says.</Typography.Paragraph>
        <FormUI.Label htmlFor="compute-template">Template</FormUI.Label>
        <SimpleSelect componentId="compute.new.template" id="compute-template" value={chosen} onChange={(e) => setTemplate(e.target.value)}>
          {(templates.data ?? []).map((t) => <SimpleSelectOption key={t.id} value={t.id}>{t.name} · {t.machine}</SimpleSelectOption>)}
        </SimpleSelect>
      </Modal>
    </>
  );
}
