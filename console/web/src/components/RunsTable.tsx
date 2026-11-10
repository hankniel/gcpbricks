import { Link } from "react-router-dom";
import {
  TableCell, TableRow, Tag, Typography, WorkflowsIcon, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "./Table";
import { RunSummary } from "../api";
import { Status } from "./Status";
import { duration, runState, when } from "../util";

export type RunRow = RunSummary & { workflow: string; job?: string };

/** Databricks' run list: start time, (job,) duration, status, error, parameters. */
export default function RunsTable({ runs, showJob }: { runs: RunRow[]; showJob?: boolean }) {
  const { theme } = useDesignSystemTheme();
  return (
    <Table scrollable>
      <TableRow isHeader>
        <TableHeader componentId="runs.h.start">Start time</TableHeader>
        {showJob && <TableHeader componentId="runs.h.name">Name</TableHeader>}
        {showJob && <TableHeader componentId="runs.h.type">Type</TableHeader>}
        {!showJob && <TableHeader componentId="runs.h.id">Run ID</TableHeader>}
        <TableHeader componentId="runs.h.launched">Launched</TableHeader>
        <TableHeader componentId="runs.h.duration" align="right">Duration</TableHeader>
        <TableHeader componentId="runs.h.status">Status</TableHeader>
        <TableHeader componentId="runs.h.error">Error</TableHeader>
        <TableHeader componentId="runs.h.params">Run parameters</TableHeader>
      </TableRow>
      {runs.map((r) => (
        <TableRow key={r.id}>
          <TableCell><Link to={`/jobs/${r.workflow}/runs/${r.id}`}>{when(r.start, true)}</Link></TableCell>
          {showJob && <TableCell><Link to={`/jobs/${r.workflow}`}>{r.workflow}</Link></TableCell>}
          {showJob && <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.xs, alignItems: "center" }}><WorkflowsIcon />Job</span></TableCell>}
          {!showJob && <TableCell><Typography.Text ellipsis>{r.id.slice(0, 13)}</Typography.Text></TableCell>}
          <TableCell>{r.repairs ? `Repaired (${r.repairs})` : r.repair_id ? `Repair ${r.repair_id}` : r.skipped ? "Skipped (busy)" : "Run"}</TableCell>
          <TableCell css={{ justifyContent: "flex-end" }}>{duration(r.start, r.end)}</TableCell>
          <TableCell><Status state={runState(r)} /></TableCell>
          <TableCell><Typography.Text ellipsis>{r.error_message}</Typography.Text></TableCell>
          <TableCell>
            <span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.xs, flexWrap: "wrap" }}>
              {Object.entries(r.params).map(([k, v]) => <Tag key={k} componentId="runs.param">{k}: {v}</Tag>)}
            </span>
          </TableCell>
        </TableRow>
      ))}
    </Table>
  );
}
