import { useState } from "react";
import { Alert, Spacer, TableSkeleton, Typography, useDesignSystemTheme } from "@databricks/design-system";
import { api } from "../api";
import { NotebookView } from "./NotebookOutputs";
import { Status } from "./Status";
import { duration, useLoad } from "../util";

const live = (s?: string) => s === "RUNNING" || s === "QUEUED";

/** A notebook_task's run: its Colab execution, and once it ran, the executed notebook with
 *  every cell's outputs, as Databricks shows a notebook task's run. */
export default function NotebookRun({ wf, id, task }: { wf: string; id: string; task: string }) {
  const { theme } = useDesignSystemTheme();
  const [running, setRunning] = useState(false);
  const { data, error } = useLoad(async () => {
    const r = await api.taskNotebook(wf, id, task);
    setRunning(live(r.run?.state));
    return r;
  }, [wf, id, task], running ? 15000 : 0);
  if (error) return <Alert componentId="nbrun.error" type="error" message="Couldn't read the notebook run" description={error} closable={false} />;
  if (!data) return <TableSkeleton lines={4} />;
  if (!data.run) return <Typography.Text color="secondary">No notebook execution yet: the runner starts one when the task runs.</Typography.Text>;
  return (
    <>
      <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.md, flexWrap: "wrap" }}>
        <Status state={data.run.state} />
        <Typography.Text color="secondary">{data.run.id}{data.run.start ? ` · ${duration(data.run.start, data.run.end)}` : ""}</Typography.Text>
        <Typography.Link componentId="nbrun.colab" href={data.run.link} openInNewTab>Open in Colab Enterprise</Typography.Link>
      </div>
      {data.run.error && <><Spacer size="sm" /><Alert componentId="nbrun.run.error" type="error" message="The notebook failed" description={data.run.error} closable={false} /></>}
      <Spacer size="sm" />
      {data.notebook ? <NotebookView cells={data.notebook.cells} />
        : <Typography.Text color="secondary">{live(data.run.state) ? "Running: its outputs appear here when it ends." : "This execution wrote no notebook."}</Typography.Text>}
    </>
  );
}
