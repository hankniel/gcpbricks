import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Alert, Button, DropdownMenu, Empty, Input, OverflowIcon, PlayIcon, SearchIcon, SegmentedControlButton,
  SegmentedControlGroup, Spacer, TableCell, TableRow, TableSkeleton, Tabs, Tag, Tooltip, Typography,
  UserIcon, WorkflowsIcon, useDesignSystemTheme,
} from "@databricks/design-system";
import PageTitle from "../components/PageTitle";
import { Table, TableHeader } from "../components/Table";
import { api, Job } from "../api";
import { RetiredTag, useShowRetired } from "../components/Retired";
import RunsTable, { RunRow } from "../components/RunsTable";
import { StatusIcon } from "../components/Status";
import { label, runState, useLoad, when } from "../util";

function trigger(j: Job): string {
  if (j.schedule) return `${j.schedule.paused ? "Paused - " : ""}Scheduled`;
  if (j.trigger) return `${j.trigger.paused ? "Paused - " : ""}Table update`;
  return "";
}

export default function JobsPage({ tab }: { tab: "jobs" | "runs" }) {
  const { theme } = useDesignSystemTheme();
  const nav = useNavigate();
  const { data, error, reload } = useLoad(api.jobs, [], 30000);
  const [q, setQ] = useState("");
  const [target, setTarget] = useState("all");
  const retired = useShowRetired("jobs.retired");
  const jobs = (data ?? []).filter((j) => retired.keep(j)
    && `${j.name} ${j.job} ${j.workflow}`.toLowerCase().includes(q.toLowerCase()) && (target === "all" || j.target === target));
  const targets = [...new Set((data ?? []).map((j) => j.target))].sort();
  const runs: RunRow[] = jobs.flatMap((j) => j.runs.map((r) => ({ ...r, workflow: j.workflow, job: j.job })))
    .sort((a, b) => b.start.localeCompare(a.start));
  const runNow = async (wf: string) => {
    const r = await api.runNow(wf, {});
    nav(`/jobs/${wf}/runs/${r.execution}`);
  };
  return (
    <>
      <PageTitle title="Jobs & Pipelines" />
      <Spacer size="md" />
      <Tabs.Root componentId="jobs.tabs" value={tab} onValueChange={(v) => nav(v === "runs" ? "/runs" : "/")}>
        <Tabs.List>
          <Tabs.Trigger value="jobs">Jobs & pipelines</Tabs.Trigger>
          <Tabs.Trigger value="runs">Runs</Tabs.Trigger>
        </Tabs.List>
      </Tabs.Root>
      <div css={{ display: "flex", gap: theme.spacing.sm, alignItems: "center", flexWrap: "wrap" }}>
        <div css={{ width: 260 }}>
          <Input componentId="jobs.filter" prefix={<SearchIcon />} placeholder="Filter by name or ID..."
                 value={q} onChange={(e) => setQ(e.target.value)} allowClear />
        </div>
        <SegmentedControlGroup componentId="jobs.target" name="target" value={target}
                               onChange={(e) => setTarget(e.target.value)}>
          <SegmentedControlButton value="all">All</SegmentedControlButton>
          {targets.map((t) => <SegmentedControlButton key={t} value={t}>{t}</SegmentedControlButton>)}
        </SegmentedControlGroup>
        {retired.toggle}
      </div>
      <Spacer size="md" />
      {error && <Alert componentId="jobs.error" type="error" message="Couldn't load jobs" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={5} />}
      {data && tab === "runs" && (runs.length ? <RunsTable runs={runs} showJob /> :
        <Empty title="No runs" description="Runs of every job appear here." />)}
      {data && tab === "jobs" && (
        <Table scrollable empty={jobs.length === 0 ? (
          <Empty title={q ? "No matching jobs" : "No jobs yet"}
                 description={q ? "Try a different name." : "A job appears here once its bundle is deployed."} />
        ) : undefined}>
          <TableRow isHeader>
            <TableHeader componentId="jobs.h.name">Name</TableHeader>
            <TableHeader componentId="jobs.h.type">Type</TableHeader>
            <TableHeader componentId="jobs.h.tags">Tags</TableHeader>
            <TableHeader componentId="jobs.h.runas">Run as</TableHeader>
            <TableHeader componentId="jobs.h.trigger">Trigger</TableHeader>
            <TableHeader componentId="jobs.h.recent">Recent runs</TableHeader>
            <TableHeader componentId="jobs.h.actions" css={{ flex: "0 0 80px" }}> </TableHeader>
          </TableRow>
          {jobs.map((j) => {
            const recent = j.runs.slice(0, 5).reverse();
            const pad = Array.from({ length: 5 - recent.length });
            const start = j.schedule ?? j.trigger;
            return (
              <TableRow key={j.workflow}>
                <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.xs, alignItems: "center", minWidth: 0 }}>
                  <Link to={`/jobs/${j.workflow}`} title={j.workflow}>{j.name}</Link><RetiredTag retired={j.retired} /></span></TableCell>
                <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.xs, alignItems: "center" }}><WorkflowsIcon />Job</span></TableCell>
                <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.xs }}>
                  <Tag componentId="jobs.tag">{j.bundle}</Tag><Tag componentId="jobs.tag">{j.target}</Tag></span></TableCell>
                <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.xs, alignItems: "center", minWidth: 0 }}>
                  <UserIcon /><Typography.Text ellipsis>{j.run_as.split("@")[0]}</Typography.Text></span></TableCell>
                <TableCell><Typography.Text ellipsis>{trigger(j)}</Typography.Text></TableCell>
                <TableCell>
                  <span css={{ display: "flex", gap: 8, alignItems: "center", height: 24, lineHeight: 0 }}>
                    {pad.map((_, i) => <span key={i} css={{ width: 14, height: 1, background: theme.colors.textPlaceholder }} />)}
                    {recent.map((r) => (
                      <Tooltip key={r.id} componentId="jobs.recent" content={`${label[runState(r)] ?? r.state}: ${when(r.start, true)}`}>
                        <Link to={`/jobs/${j.workflow}/runs/${r.id}`} aria-label={`${label[runState(r)] ?? r.state} run`} css={{ display: "inline-flex" }}>
                          <StatusIcon state={runState(r)} />
                        </Link>
                      </Tooltip>
                    ))}
                  </span>
                </TableCell>
                <TableCell css={{ flex: "0 0 80px", justifyContent: "flex-end" }}>
                  {!j.retired && <Button componentId="jobs.row.run" size="small" icon={<PlayIcon />} aria-label="Run now"
                          onClick={() => runNow(j.workflow)} />}
                  <DropdownMenu.Root>
                    <DropdownMenu.Trigger asChild>
                      <Button componentId="jobs.row.more" size="small" icon={<OverflowIcon />} aria-label="More actions" />
                    </DropdownMenu.Trigger>
                    <DropdownMenu.Content align="end">
                      <DropdownMenu.Item componentId="jobs.row.open" onClick={() => nav(`/jobs/${j.workflow}`)}>View job</DropdownMenu.Item>
                      {start && <DropdownMenu.Item componentId="jobs.row.pause"
                        onClick={() => (start.paused ? api.resume(j.workflow) : api.pause(j.workflow)).then(reload)}>
                        {start.paused ? "Resume trigger" : "Pause trigger"}</DropdownMenu.Item>}
                    </DropdownMenu.Content>
                  </DropdownMenu.Root>
                </TableCell>
              </TableRow>
            );
          })}
        </Table>
      )}
    </>
  );
}
