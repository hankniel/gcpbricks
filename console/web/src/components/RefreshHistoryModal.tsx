import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Alert, Button, Modal, RefreshIcon, Spacer, TableCell, TableRow, TableSkeleton, Typography } from "@databricks/design-system";
import { api, QualityMonitor } from "../api";
import { duration, runState, useLoad, when } from "../util";
import { Status } from "./Status";
import { Table, TableHeader } from "./Table";

/** Databricks' "View refresh history": the monitor's refreshes, newest first, and Refresh metrics,
 *  which starts one. A refresh is a run of the monitor's job. */
export default function RefreshHistoryModal({ m, open, onClose }: { m: QualityMonitor; open: boolean; onClose: () => void }) {
  const nav = useNavigate();
  const { data, error } = useLoad(() => (open ? api.job(m.workflow) : Promise.resolve(null)), [open, m.workflow]);
  const [starting, setStarting] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const refresh = async () => {
    setStarting(true); setRunError(null);
    try { const r = await api.runNow(m.workflow, {}); nav(`/jobs/${m.workflow}/runs/${r.execution}`); }
    catch (e) { setRunError((e as Error).message); setStarting(false); }
  };
  return (
    <Modal componentId="quality.history.modal" title="Refresh history" visible={open} onCancel={onClose} size="wide"
           footer={<Button componentId="quality.history.close" onClick={onClose}>Close</Button>}>
      <div css={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
        <Typography.Text color="secondary">
          Each refresh recomputes the metric tables. It runs as the job <Link to={`/jobs/${m.workflow}`}>{m.job}</Link>.
        </Typography.Text>
        <Button componentId="quality.history.refresh" type="primary" icon={<RefreshIcon />} loading={starting} onClick={refresh}>
          Refresh metrics
        </Button>
      </div>
      {runError && <><Spacer size="sm" /><Alert componentId="quality.history.run.error" type="error" message="Couldn't start the refresh" description={runError} closable={false} /></>}
      <Spacer size="md" />
      {error && <Alert componentId="quality.history.error" type="error" message="Couldn't read the refreshes" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={5} />}
      {data && !data.runs.length && <Typography.Text color="secondary">No refresh has run yet.</Typography.Text>}
      {data && data.runs.length > 0 && (
        <Table scrollable>
          <TableRow isHeader>
            <TableHeader componentId="quality.history.h.start">Start time</TableHeader>
            <TableHeader componentId="quality.history.h.duration">Duration</TableHeader>
            <TableHeader componentId="quality.history.h.status">Status</TableHeader>
            <TableHeader componentId="quality.history.h.error">Error</TableHeader>
          </TableRow>
          {data.runs.map((r) => (
            <TableRow key={r.id}>
              <TableCell><Link to={`/jobs/${m.workflow}/runs/${r.id}`}>{when(r.start, true)}</Link></TableCell>
              <TableCell>{duration(r.start, r.end)}</TableCell>
              <TableCell><Status state={runState(r)} /></TableCell>
              <TableCell><Typography.Text ellipsis>{r.error_message}</Typography.Text></TableCell>
            </TableRow>
          ))}
        </Table>
      )}
    </Modal>
  );
}
