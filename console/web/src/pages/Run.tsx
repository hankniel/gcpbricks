import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  Alert, Button, DangerModal, SimpleSelect, SimpleSelectOption, Spacer, TableCell, TableRow, TableSkeleton, Tabs, Tag,
  Tooltip, Typography, WorkflowsIcon,
  useDesignSystemTheme,
} from "@databricks/design-system";
import PageTitle from "../components/PageTitle";
import SidePanel from "../components/SidePanel";
import { Table, TableHeader } from "../components/Table";
import { api } from "../api";
import { Field, ParamList, Section } from "../components/Panel";
import { Status, useStateFill } from "../components/Status";
import RepairModal from "../components/RepairModal";
import WhyFailed from "../components/WhyFailed";
import TaskGraph from "../components/TaskGraph";
import { useToast } from "../components/Toast";
import { duration, KIND, label, live, pollMs, runState, useLoad, useTick, when } from "../util";

export default function RunPage() {
  const { wf = "", id = "" } = useParams();
  const nav = useNavigate();
  // Which attempt of the run (0: the original, n: repair n); none: its latest.
  const [search, setSearch] = useSearchParams();
  const wanted = search.get("repair_id");
  const repairId = wanted === null ? null : Number(wanted);
  const { theme } = useDesignSystemTheme();
  const fill = useStateFill();
  const job = useLoad(() => api.job(wf), [wf]);
  // While the run moves, poll every few seconds and let its clocks tick each second.
  const [moving, setMoving] = useState(true);
  const run = useLoad(() => api.run(wf, id, repairId), [wf, id, repairId], pollMs(moving));
  const r = run.data;
  // An attempt's own execution opens as its run, that attempt picked.
  useEffect(() => {
    if (r && r.run_id !== id) nav(`/jobs/${wf}/runs/${r.run_id}?repair_id=${r.repair_id}`, { replace: true });
  }, [r, id, wf, nav]);
  const attempt = (n: number) => setSearch(n === r?.latest_repair_id ? {} : { repair_id: String(n) });
  const attemptName = (n: number) => (n === 0 ? "Original run" : `Repair ${n}`);
  useEffect(() => setMoving(!r || live(r.state) || Object.values(r.task_runs).some((x) => live(x.status))), [r]);
  useTick(moving);
  const notify = useToast();
  const [view, setView] = useState<"graph" | "timeline" | "list">("graph");
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [repairing, setRepairing] = useState(false);
  // A task opens in the attempt that ran it: a reused one, in the earlier attempt it ran in.
  const pick = useCallback((key: string) => {
    const ran = r?.task_runs[key]?.ran_in ?? r?.repair_id;
    const ex = r?.repair_history.find((h) => h.repair_id === ran)?.execution || r?.id || id;
    nav(`/jobs/${wf}/runs/${ex}/tasks/${encodeURIComponent(key)}`);
  }, [nav, wf, id, r]);
  const cancel = async () => {
    setConfirmCancel(false);
    try { await api.cancel(wf, r!.id); notify({ title: "Run cancelled", severity: "info" }); run.reload(); }
    catch (e) { notify({ title: "Couldn't cancel the run", description: (e as Error).message, severity: "error" }); }
  };
  const tasks = r ? Object.values(r.task_runs) : [];
  const nodes = job.data?.graph.nodes ?? [];
  const t0 = r ? Date.parse(r.start) : 0;
  const span = r ? Math.max(1, (r.end ? Date.parse(r.end) : Date.now()) - t0) : 1;
  return (
    <>
      <PageTitle crumbs={[{ to: "/", label: "Jobs & Pipelines" }, { to: `/jobs/${wf}`, label: job.data?.name ?? wf }]}
        icon={<WorkflowsIcon />} title={`${job.data?.name ?? wf} run`}
        addOns={r && r.repair_history.length > 1 && (
          <SimpleSelect componentId="run.attempt" id="run-attempt" value={String(r.repair_id)} css={{ minWidth: 220 }}
                        onChange={(e) => attempt(Number(e.target.value))}>
            {[...r.repair_history].reverse().map((h) => (
              <SimpleSelectOption key={h.repair_id} value={String(h.repair_id)} disabled={!h.execution}>
                {attemptName(h.repair_id)} · {h.execution ? (label[h.state ?? ""] ?? h.state ?? "") : "Starting"}
                {h.repair_id === r.latest_repair_id ? " (latest)" : ""}
              </SimpleSelectOption>))}
          </SimpleSelect>
        )}
        actions={r && (
          <>
            {r.state === "ACTIVE" && <Button componentId="run.cancel" onClick={() => setConfirmCancel(true)}>Cancel run</Button>}
            {(r.state === "FAILED" || r.state === "CANCELLED" || r.with_failures) && r.repair_id === r.latest_repair_id && <Button componentId="run.repair" type="primary"
              onClick={() => setRepairing(true)}>Repair run</Button>}
          </>
        )} />
      {run.error && <Alert componentId="run.error" type="error" message="Couldn't load this run" description={run.error} closable={false} />}
      {r && r.repair_id !== r.latest_repair_id && <><Spacer size="sm" /><Alert componentId="run.earlier" type="info" closable={false}
        message={<>You're looking at {attemptName(r.repair_id).toLowerCase()} of this run. <Typography.Link componentId="run.latest"
          onClick={() => attempt(r.latest_repair_id)}>View {attemptName(r.latest_repair_id).toLowerCase()} (latest)</Typography.Link></>} />
        </>}
      {(!r || !job.data) && !run.error && <><Spacer size="md" /><TableSkeleton lines={6} /></>}
      {r && job.data && (
        // Databricks' run page: the view tabs right under the title, the run's details beside from the same line.
        <SidePanel>
          <div css={{ minWidth: 0 }}>
            <Tabs.Root componentId="run.view" value={view} onValueChange={(v) => setView(v as typeof view)} css={{ marginTop: 4 }}>
              <Tabs.List css={{ "&&": { marginBottom: 2 } }}>
                <Tabs.Trigger value="graph">Graph</Tabs.Trigger>
                <Tabs.Trigger value="timeline">Timeline</Tabs.Trigger>
                <Tabs.Trigger value="list">List</Tabs.Trigger>
              </Tabs.List>
            </Tabs.Root>
            {(r.state === "FAILED" || tasks.some((x) => x.excluded)) && <Spacer size="md" />}
            {r.state === "FAILED" && <WhyFailed wf={wf} id={r.id} start={r.start} />}
            {tasks.some((x) => x.excluded) && <><Alert componentId="run.excluded" type="warning" closable={false}
              message="Succeeded, but some pipeline steps didn't run"
              description={<>A condition in the pipeline didn't hold, as when a gate rejects a model:{" "}
                {tasks.filter((x) => x.excluded).map((x, i) => <span key={x.key}>{i ? "; " : ""}
                  <Typography.Link componentId="run.excluded.task" onClick={() => pick(x.key)}>{x.key}</Typography.Link>
                  {" "}skipped {x.excluded!.join(", ")}</span>)}.</>} />
              <Spacer size="md" /></>}
            {view !== "graph" && r.state !== "FAILED" && !tasks.some((x) => x.excluded) && <Spacer size="md" />}
            {view === "graph" && (
              <TaskGraph nodes={nodes} edges={job.data.graph.edges} height={Math.max(420, window.innerHeight - 190)} onPick={pick}
                         status={Object.fromEntries(nodes.map((n) => [n.key, r.task_runs[n.key]?.status ?? null]))}
                         timing={Object.fromEntries(tasks.map((x) => [x.key, { start: x.start, end: x.end }]))} />
            )}
            {view === "timeline" && (
              <div>
                {nodes.map((n) => {
                  const x = r.task_runs[n.key];
                  const a = x?.start ? (Date.parse(x.start) - t0) / span : 0;
                  const b = x?.start ? ((x.end ? Date.parse(x.end) : Date.now()) - t0) / span : 0;
                  return (
                    <div key={n.key} onClick={() => pick(n.key)} css={{ display: "grid", gridTemplateColumns: "200px 1fr 90px", gap: theme.spacing.sm,
                      alignItems: "center", height: 32, cursor: "pointer", borderBottom: `1px solid ${theme.colors.border}`,
                      "&:hover": { background: theme.colors.tableBackgroundUnselectedHover } }}>
                      <Typography.Text ellipsis>{n.key}</Typography.Text>
                      <div css={{ position: "relative", height: 14 }}>
                        {x?.start && <Tooltip componentId="run.tl" content={`${label[x.status ?? ""] ?? ""} · ${duration(x.start, x.end)}`}>
                          <span css={{ position: "absolute", top: 0, bottom: 0, left: `${a * 100}%`, width: `${Math.max(0.5, (b - a) * 100)}%`,
                                       background: fill(x.status), borderRadius: 3 }} />
                        </Tooltip>}
                      </div>
                      <Typography.Text color="secondary" css={{ textAlign: "right" }}>{x?.start ? duration(x.start, x.end) : label[x?.status ?? ""] ?? ""}</Typography.Text>
                    </div>
                  );
                })}
              </div>
            )}
            {view === "list" && (
              <Table scrollable>
                <TableRow isHeader>
                  <TableHeader componentId="list.h.status">Status</TableHeader>
                  <TableHeader componentId="list.h.name">Name</TableHeader>
                  <TableHeader componentId="list.h.type">Type</TableHeader>
                  <TableHeader componentId="list.h.resource">Resource</TableHeader>
                  <TableHeader componentId="list.h.duration" align="right">Duration</TableHeader>
                  <TableHeader componentId="list.h.depends">Depends on</TableHeader>
                </TableRow>
                {nodes.map((n) => {
                  const x = r.task_runs[n.key];
                  return (
                    <TableRow key={n.key}>
                      <TableCell><Status state={x?.status ?? "EXCLUDED"} /></TableCell>
                      <TableCell><Typography.Link componentId="list.task" onClick={() => pick(n.key)}>{n.key}</Typography.Link></TableCell>
                      <TableCell>{KIND[n.kind] ?? n.kind}</TableCell>
                      <TableCell><Typography.Text ellipsis>{n.detail}</Typography.Text></TableCell>
                      <TableCell css={{ justifyContent: "flex-end" }}>{x?.start ? duration(x.start, x.end) : ""}</TableCell>
                      <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.xs }}>
                        {n.depends_on.map((d) => <Tag key={d} componentId="list.dep">{d}</Tag>)}</span></TableCell>
                    </TableRow>
                  );
                })}
              </Table>
            )}
          </div>
          <div css={{ minWidth: 0 }}>
            <Section title="Job run details">
              <Field name="Job ID"><Link to={`/jobs/${wf}`}>{wf}</Link></Field>
              <Field name="Job run ID">{r.run_id}</Field>
              <Field name="Attempt">{attemptName(r.repair_id)}{r.repair_history.length > 1 ? ` of ${r.repair_history.length}` : ""}</Field>
              {r.repair_id > 0 && <Field name="Repaired by">{r.repair_history.find((h) => h.repair_id === r.repair_id)?.by || "-"}</Field>}
              <Field name="Started">{when(r.start, true)}</Field>
              <Field name="Ended">{r.end ? when(r.end, true) : "-"}</Field>
              <Field name="Duration">{duration(r.start, r.end)}</Field>
              <Field name="Status"><Status state={runState(r)} /></Field>
              {r.error_message && <Field name="Error">{r.error_message}</Field>}
            </Section>
            <Section title="Job parameters">
              <ParamList rows={Object.entries(r.params)} empty="The run used the defaults" />
              {Object.keys(r.repair_history.find((h) => h.repair_id === r.repair_id)?.overrides ?? {}).length > 0 && (
                <Typography.Hint>Overridden by repairs: {Object.keys(r.repair_history.find((h) => h.repair_id === r.repair_id)!.overrides!).join(", ")}</Typography.Hint>)}
            </Section>
          </div>
        </SidePanel>
      )}
      <DangerModal componentId="run.cancel.modal" visible={confirmCancel} title="Cancel run" okText="Cancel run" cancelText="Keep running"
                   onCancel={() => setConfirmCancel(false)} onOk={cancel}>
        This stops the run and every container it started.
      </DangerModal>
      <RepairModal wf={wf} id={id} visible={repairing} onClose={() => setRepairing(false)}
                   onStarted={() => { setSearch({}); setMoving(true); run.reload(); }} />
    </>
  );
}
