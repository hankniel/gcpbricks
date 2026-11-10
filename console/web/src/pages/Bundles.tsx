import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  Alert, Button, Empty, FolderIcon, PlayIcon, Spacer, TableCell, TableRow, TableSkeleton, Tabs, Tag,
  Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../components/Table";
import { api, type BundleDetail, type Deployment } from "../api";
import { Field, Section } from "../components/Panel";
import PageTitle from "../components/PageTitle";
import SidePanel from "../components/SidePanel";
import { Status } from "../components/Status";
import { runState, useLoad, when } from "../util";

const short = (v: string | null) => (v ? (v.length > 12 && /^[0-9a-f]+/.test(v) ? v.slice(0, 8) : v) : "-");

export function BundlesPage() {
  const { data, error } = useLoad(api.bundles, []);
  return (
    <>
      <PageTitle title="Bundles" />
      <Spacer size="md" />
      {error && <Alert componentId="bundles.error" type="error" message="Couldn't load bundles" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={4} />}
      {data && (
        <Table scrollable empty={data.length ? undefined : <Empty title="No bundles" description="A bundle appears once mlops bundle deploy has run." />}>
          <TableRow isHeader>
            <TableHeader componentId="bundles.h.bundle">Bundle</TableHeader>
            <TableHeader componentId="bundles.h.target">Target</TableHeader>
            <TableHeader componentId="bundles.h.version">Version</TableHeader>
            <TableHeader componentId="bundles.h.platform">Platform</TableHeader>
            <TableHeader componentId="bundles.h.jobs">Jobs</TableHeader>
            <TableHeader componentId="bundles.h.deployed">Deployed</TableHeader>
          </TableRow>
          {data.map((b) => (
            <TableRow key={`${b.bundle}/${b.target}`}>
              <TableCell><Link to={`/bundles/${b.bundle}?target=${b.target}`}>{b.bundle}</Link></TableCell>
              <TableCell><Tag componentId="bundles.target">{b.target}</Tag></TableCell>
              <TableCell><Typography.Text code>{short(b.version)}</Typography.Text></TableCell>
              <TableCell>{b.mlops_version ?? <Typography.Text color="secondary">before 0.5.2</Typography.Text>}</TableCell>
              <TableCell>{b.jobs}</TableCell>
              <TableCell>{when(b.deployed_at, true)}{b.deployed_by ? ` by ${b.deployed_by}` : ""}</TableCell>
            </TableRow>
          ))}
        </Table>
      )}
    </>
  );
}

/** Every resource the deploy recorded, each linked to where the console shows it. */
function Resources({ d }: { d: Deployment }) {
  const r = d.resources;
  const rows: { kind: string; key: string; name: string; to?: string }[] = [
    ...Object.entries(r.jobs).map(([k, wf]) => ({ kind: "Job", key: k, name: wf, to: `/jobs/${wf}` })),
    ...Object.keys(r.dashboards).map((k) => ({ kind: "Dashboard", key: k, name: k, to: `/dashboards/${d.bundle}/${d.target}/${k}` })),
    ...Object.entries(r.experiments).map(([k, n]) => ({ kind: "Experiment", key: k, name: n, to: `/experiments/${n}` })),
    ...Object.entries(r.registered_models).map(([k, n]) => ({ kind: "Registered model", key: k, name: n, to: "/models" })),
    ...Object.entries(r.model_serving_endpoints).map(([k, n]) => ({ kind: "Serving endpoint", key: k, name: n, to: "/serving" })),
    ...Object.entries(r.schemas).map(([k, s]) => ({ kind: "Schema", key: k, name: `${s.project}.${s.dataset}`,
      to: `/catalog/${s.dataset}${s.project !== d.project ? `?project=${s.project}` : ""}` })),
  ];
  return (
    <Table>
      <TableRow isHeader>
        <TableHeader componentId="bundle.res.kind">Type</TableHeader>
        <TableHeader componentId="bundle.res.key">Key</TableHeader>
        <TableHeader componentId="bundle.res.name">Name on {d.target}</TableHeader>
      </TableRow>
      {rows.map((x) => (
        <TableRow key={`${x.kind}/${x.key}`}>
          <TableCell>{x.kind}</TableCell>
          <TableCell>{x.key}</TableCell>
          <TableCell>{x.to ? <Link to={x.to}>{x.name}</Link> : x.name}</TableCell>
        </TableRow>
      ))}
    </Table>
  );
}

function Target({ t, onRun }: { t: BundleDetail["targets"][number]; onRun: (wf: string) => void }) {
  const { theme } = useDesignSystemTheme();
  const d = t.deployment;
  return (
    <SidePanel>
      <div>
        <Typography.Title level={4}>Jobs</Typography.Title>
        <Table empty={t.jobs.length ? undefined : <Empty description="No jobs deployed on this target." />}>
          <TableRow isHeader>
            <TableHeader componentId="bundle.jobs.name">Job</TableHeader>
            <TableHeader componentId="bundle.jobs.start">Starts</TableHeader>
            <TableHeader componentId="bundle.jobs.last">Last run</TableHeader>
            <TableHeader componentId="bundle.jobs.run">{""}</TableHeader>
          </TableRow>
          {t.jobs.map((j) => {
            const start = j.schedule ?? j.trigger;
            const last = j.runs[0];
            return (
              <TableRow key={j.workflow}>
                <TableCell><Link to={`/jobs/${j.workflow}`}>{j.workflow}</Link></TableCell>
                <TableCell>{start ? `${j.schedule ? start.cron : "Trigger"}${start.paused ? " (paused)" : ""}` : "Manual"}</TableCell>
                <TableCell>{last ? <span css={{ display: "inline-flex", alignItems: "center", gap: theme.spacing.sm }}>
                  <Status state={runState(last)} /><Typography.Text color="secondary">{when(last.start)}</Typography.Text></span> : "-"}</TableCell>
                <TableCell><Button componentId="bundle.jobs.runnow" size="small" icon={<PlayIcon />} onClick={() => onRun(j.workflow)}>Run now</Button></TableCell>
              </TableRow>
            );
          })}
        </Table>
        <Spacer size="lg" />
        <Typography.Title level={4}>Resources</Typography.Title>
        {d ? <Resources d={d} /> : <Typography.Text color="secondary">Recorded from gcpbricks 0.5.2: redeploy to list every resource.</Typography.Text>}
      </div>
      <div>
        <Section title="Deployment">
          {d ? <>
            <Field name="Version"><Typography.Text code>{d.version}</Typography.Text></Field>
            <Field name="Deployed">{when(d.deployed_at, true)}</Field>
            {d.deployed_by && <Field name="By">{d.deployed_by}</Field>}
            {d.ci_url && <Field name="Pipeline"><a href={d.ci_url} target="_blank" rel="noreferrer">CI pipeline</a></Field>}
            <Field name="Platform">gcpbricks {d.mlops_version}{d.declared_mlops_version !== d.mlops_version ? ` (declares ${d.declared_mlops_version})` : ""}</Field>
            <Field name="Mode">{d.mode ?? "-"}</Field>
          </> : <Typography.Text color="secondary">No deployment record.</Typography.Text>}
        </Section>
        {d && (
          <Section title="Where">
            <Field name="Run as">{d.run_as}</Field>
            <Field name="Project">{d.project}</Field>
            <Field name="Region">{d.region}</Field>
            <Field name="Bucket">{d.bucket}</Field>
          </Section>
        )}
      </div>
    </SidePanel>
  );
}

export function BundlePage() {
  const { name = "" } = useParams();
  const nav = useNavigate();
  const search = new URLSearchParams(window.location.search);
  const { data, error } = useLoad(() => api.bundle(name), [name], 30000);
  const [msg, setMsg] = useState<string | null>(null);
  const run = async (wf: string) => {
    try { const r = await api.runNow(wf, {}); nav(`/jobs/${wf}/runs/${r.execution}`); }
    catch (e) { setMsg((e as Error).message); }
  };
  const first = data?.targets.find((t) => t.target === search.get("target"))?.target ?? data?.targets[0]?.target;
  return (
    <>
      <PageTitle crumbs={[{ to: "/bundles", label: "Bundles" }]} icon={<FolderIcon />} title={name} />
      <Spacer size="md" />
      {error && <Alert componentId="bundle.error" type="error" message="Couldn't load this bundle" description={error} closable={false} />}
      {msg && <><Alert componentId="bundle.msg" type="error" message={msg} onClose={() => setMsg(null)} /><Spacer size="sm" /></>}
      {!data && !error && <TableSkeleton lines={8} />}
      {data && first && (
        <Tabs.Root componentId="bundle.targets" defaultValue={first}>
          <Tabs.List>{data.targets.map((t) => <Tabs.Trigger key={t.target} value={t.target}>{t.target}</Tabs.Trigger>)}</Tabs.List>
          {data.targets.map((t) => (
            <Tabs.Content key={t.target} value={t.target}><Spacer size="md" /><Target t={t} onRun={run} /></Tabs.Content>))}
        </Tabs.Root>
      )}
    </>
  );
}
