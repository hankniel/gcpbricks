import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Alert, Button, DropdownMenu, Empty, FormUI, Input, Modal, ModelsIcon, OverflowIcon, SearchIcon, Spacer,
  TableCell, TableRow, TableSkeleton, Tag, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../../components/Table";
import { api, Model, ModelVersion } from "../../api";
import { Field, Section } from "../../components/Panel";
import PageTitle from "../../components/PageTitle";
import SidePanel from "../../components/SidePanel";
import TableLink from "../../components/TableLink";
import { RetiredNotice } from "../../components/Retired";
import MetricTrend from "../../components/MetricTrend";
import { ago, useLoad, when } from "../../util";

const PAGE = 25;

/** Moves an alias onto a version, or removes it; shared by the model and version pages. */
export function useAliases(id: string, reload: () => void) {
  const [aliasFor, setAliasFor] = useState<string | null>(null);
  const [alias, setAlias] = useState("champion");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; error?: boolean } | null>(null);
  const apply = async (a: string, version: string | null) => {
    setBusy(true);
    try {
      const out = await api.setAlias(id, a, version);
      setMsg({ text: version ? `@${a} now points to version ${version}${out.from.length ? ` (was version ${out.from.join(", ")})` : ""}.` : `@${a} removed.` });
      setAliasFor(null); reload();
    } catch (e) { setMsg({ text: (e as Error).message, error: true }); }
    finally { setBusy(false); }
  };
  const modal = (
    <Modal componentId="model.alias.modal" title={`Set alias on version ${aliasFor}`} visible={!!aliasFor} okText="Save"
           cancelText="Cancel" confirmLoading={busy} onCancel={() => setAliasFor(null)}
           onOk={() => aliasFor && alias.trim() && apply(alias.trim(), aliasFor)}>
      <Typography.Paragraph color="secondary">An alias names one version. Setting it here moves it off the version that holds it now.</Typography.Paragraph>
      <FormUI.Label htmlFor="alias">Alias</FormUI.Label>
      <Input id="alias" componentId="model.alias.input" value={alias} onChange={(e) => setAlias(e.target.value)} />
    </Modal>
  );
  const banner = msg && <><Alert componentId="model.msg" type={msg.error ? "error" : "info"} message={msg.text} onClose={() => setMsg(null)} /><Spacer size="sm" /></>;
  return { ask: (v: string) => { setAlias("champion"); setAliasFor(v); }, apply, modal, banner };
}

export function AliasTags({ v }: { v: ModelVersion }) {
  const { theme } = useDesignSystemTheme();
  return (
    <span css={{ display: "inline-flex", gap: theme.spacing.xs, flexWrap: "wrap" }}>
      {v.aliases.map((a) => <Tag key={a} componentId="model.alias" color="indigo">@{a}</Tag>)}
    </span>
  );
}

function matches(v: ModelVersion, q: string): boolean {
  const s = q.toLowerCase();
  return !s || v.version === s.replace(/^v(ersion)?\s*/, "") || v.aliases.some((a) => `@${a}`.includes(s))
    || Object.entries(v.labels).some(([k, val]) => `${k}:${val}`.toLowerCase().includes(s))
    || (v.source_run?.name ?? "").toLowerCase().includes(s);
}

/** Where the model's data comes from and goes: the feature and label tables its champion (else
 *  latest) version trained on, the batch serving tasks scoring it and its predictions table. */
function ModelLineage({ id, model }: { id: string; model: Model }) {
  const { theme } = useDesignSystemTheme();
  const v = model.versions.find((x) => x.aliases.includes("champion")) ?? model.versions[0];
  const lineage = useLoad(() => (v ? api.versionLineage(id, v.version) : Promise.resolve(null)), [id, v?.version]);
  const tasks = useLoad(api.batchTasks, []);
  const serving = (tasks.data ?? []).filter((t) => t.models.some((s) => s.model === model.name));
  const f = lineage.data?.flow;
  return (
    <Section title="Lineage">
      {v && <Typography.Hint>As trained for version {v.version}{v.aliases.length ? ` (@${v.aliases.join(" @")})` : ""}</Typography.Hint>}
      <Field name="Features">{lineage.data ? <TableLink name={f?.features ?? null} /> : "..."}</Field>
      <Field name="Labels">{lineage.data ? <TableLink name={f?.labels ?? null} /> : "..."}</Field>
      <Field name="Trained on">{f?.train_start || f?.train_cutoff ? `${f.train_start ?? "?"} to ${f.train_cutoff ?? "?"}` : "-"}</Field>
      <Field name="Batch serving">{tasks.data ? (serving.length ? (
        <span css={{ display: "inline-flex", flexDirection: "column", gap: theme.spacing.xs }}>
          {serving.map((t) => <Link key={`${t.workflow}/${t.task}`} to={`/batch-serving/${t.workflow}/${encodeURIComponent(t.task)}`}>
            {t.workflow} / {t.task}</Link>)}</span>) : "Not batch served") : "..."}</Field>
      <Field name="Predictions">{lineage.data ? <TableLink name={f?.predictions ?? null} /> : "..."}</Field>
      {v && <Typography.Text size="sm"><Link to={`/models/${id}/versions/${v.version}`}>Full lineage of version {v.version}</Link></Typography.Text>}
    </Section>
  );
}

/** Databricks' registered model page: its versions, with aliases, tags and source runs; details beside. */
export default function ModelPage() {
  const { id = "" } = useParams();
  const { theme } = useDesignSystemTheme();
  const { data: m, error, reload } = useLoad<Model>(() => api.model(id), [id]);
  const aliases = useAliases(id, reload);
  const [q, setQ] = useState("");
  const [shown, setShown] = useState(PAGE);
  const rows = (m?.versions ?? []).filter((v) => matches(v, q));
  const held = (m?.versions ?? []).flatMap((v) => v.aliases.map((a) => [a, v.version] as const));
  return (
    <>
      <PageTitle crumbs={[{ to: "/models", label: "Models" }]} icon={<ModelsIcon />} title={m?.name ?? id}
        actions={m && (
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <Button componentId="model.more" icon={<OverflowIcon />} aria-label="More actions" />
            </DropdownMenu.Trigger>
            <DropdownMenu.Content align="end">
              <DropdownMenu.Item componentId="model.copy" onClick={() => navigator.clipboard.writeText(m.id)}>Copy model ID</DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu.Root>
        )} />
      <Spacer size="md" />
      {error && <Alert componentId="model.error" type="error" message="Couldn't load this model" description={error} closable={false} />}
      {aliases.banner}
      {!m && !error && <TableSkeleton lines={8} />}
      {m && <RetiredNotice kind="model" name={m.name} retired={m.retired} remove={() => api.deleteModel(m.id)} back="/models" />}
      {m && (
        <SidePanel>
          <div css={{ minWidth: 0 }}>
            <Typography.Title level={4}>Description</Typography.Title>
            <Typography.Paragraph color={m.description ? undefined : "secondary"}>{m.description || "No description"}</Typography.Paragraph>
            <Spacer size="sm" />
            <MetricTrend id={id} versions={m.versions} />
            <Spacer size="md" />
            <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: theme.spacing.md }}>
              <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>Versions <Typography.Text color="secondary">({m.versions.length})</Typography.Text></Typography.Title>
              <div css={{ width: 320 }}>
                <Input componentId="model.versions.filter" prefix={<SearchIcon />} placeholder="Filter by version, alias, tag or run"
                       value={q} onChange={(e) => { setQ(e.target.value); setShown(PAGE); }} allowClear aria-label="Filter versions" />
              </div>
            </div>
            <Spacer size="sm" />
            <Table scrollable empty={rows.length ? undefined : <Empty title="No matching versions" description="No version matches the filter. Clear it to see them all." />}>
              <TableRow isHeader>
                <TableHeader componentId="model.h.version" css={{ flex: "0 0 110px" }}>Version</TableHeader>
                <TableHeader componentId="model.h.created">Registered at</TableHeader>
                <TableHeader componentId="model.h.aliases">Aliases</TableHeader>
                <TableHeader componentId="model.h.run">Source run</TableHeader>
                <TableHeader componentId="model.h.tags">Tags</TableHeader>
                <TableHeader componentId="model.h.serving">Serving</TableHeader>
                <TableHeader componentId="model.h.actions" css={{ flex: "0 0 48px" }}> </TableHeader>
              </TableRow>
              {rows.slice(0, shown).map((v) => (
                <TableRow key={v.version}>
                  <TableCell css={{ flex: "0 0 110px" }}><Link to={`/models/${id}/versions/${v.version}`}>Version {v.version}</Link></TableCell>
                  <TableCell><span title={when(v.created, true)}>{when(v.created, true)}</span></TableCell>
                  <TableCell><AliasTags v={v} /></TableCell>
                  <TableCell>{v.source_run ? <Link css={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={v.source_run.name} to={`/experiments/${v.source_run.experiment}/runs/${v.source_run.id}`}>
                    {v.source_run.name}</Link> : <Typography.Text color="secondary">-</Typography.Text>}</TableCell>
                  <TableCell><span css={{ display: "flex", gap: theme.spacing.xs, alignItems: "center", minWidth: 0, whiteSpace: "nowrap", overflow: "hidden" }}>
                    {Object.entries(v.labels).slice(0, 2).map(([k, val]) => <Tag key={k} componentId="model.label">{k}: {val}</Tag>)}
                    {Object.keys(v.labels).length > 2 && <Typography.Text color="secondary">+{Object.keys(v.labels).length - 2}</Typography.Text>}</span></TableCell>
                  <TableCell>{v.serving.length ? v.serving.map((e) => <Link key={e.id} to={`/serving/${e.id}`}><Tag componentId="model.serving">{e.name} · {e.traffic}%</Tag></Link>)
                    : <Typography.Text color="secondary">-</Typography.Text>}</TableCell>
                  <TableCell css={{ flex: "0 0 48px" }}>
                    <DropdownMenu.Root>
                      <DropdownMenu.Trigger asChild>
                        <Button componentId="model.version.more" size="small" icon={<OverflowIcon />} aria-label={`Actions for version ${v.version}`} />
                      </DropdownMenu.Trigger>
                      <DropdownMenu.Content align="end">
                        <DropdownMenu.Item componentId="model.version.alias" onClick={() => aliases.ask(v.version)}>Set alias</DropdownMenu.Item>
                        {v.aliases.map((a) => <DropdownMenu.Item key={a} componentId="model.version.unalias" onClick={() => aliases.apply(a, null)}>Remove @{a}</DropdownMenu.Item>)}
                        <DropdownMenu.Separator />
                        <DropdownMenu.Item componentId="model.version.copy" onClick={() => navigator.clipboard.writeText(v.artifact_uri)}>Copy artifact location</DropdownMenu.Item>
                      </DropdownMenu.Content>
                    </DropdownMenu.Root>
                  </TableCell>
                </TableRow>
              ))}
            </Table>
            {rows.length > shown && <><Spacer size="sm" />
              <Button componentId="model.versions.more" onClick={() => setShown(shown + PAGE)}>Show {Math.min(PAGE, rows.length - shown)} more of {rows.length - shown}</Button></>}
          </div>
          <div>
            <Section title="About this model">
              <Field name="Model ID">{m.id}</Field>
              <Field name="Created">{when(m.created, true)}</Field>
              <Field name="Last modified"><span title={when(m.updated, true)}>{ago(m.updated)}</span></Field>
              <Field name="Latest version">{m.versions[0] ? <Link to={`/models/${id}/versions/${m.versions[0].version}`}>Version {m.versions[0].version}</Link> : "-"}</Field>
              <Field name="Experiment">{m.experiment ? <Link to={`/experiments/${m.experiment}`}>{m.experiment}</Link> : "-"}</Field>
              <Field name="Registry">Vertex AI Model Registry</Field>
            </Section>
            <ModelLineage id={id} model={m} />
            <Section title="Aliases">
              {held.length ? held.map(([a, v]) => <Field key={a} name={`@${a}`}><Link to={`/models/${id}/versions/${v}`}>Version {v}</Link></Field>)
                : <Typography.Text color="secondary">No aliases. Set one from a version's menu.</Typography.Text>}
            </Section>
            <Section title="Tags">
              {Object.keys(m.labels).length ? Object.entries(m.labels).map(([k, v]) => <Field key={k} name={k}>{v}</Field>)
                : <Typography.Text color="secondary">No tags</Typography.Text>}
            </Section>
          </div>
        </SidePanel>
      )}
      {aliases.modal}
    </>
  );
}
