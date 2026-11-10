import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Alert, Empty, Input, SearchIcon, Spacer, TableCell, TableRow, TableSkeleton, Typography,
  useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../../components/Table";
import { api } from "../../api";
import PageTitle from "../../components/PageTitle";
import { RetiredTag, useShowRetired } from "../../components/Retired";
import { useLoad, when } from "../../util";

export default function ExperimentsPage() {
  const { data, error } = useLoad(api.experiments, []);
  const { theme } = useDesignSystemTheme();
  const [q, setQ] = useState("");
  const retired = useShowRetired("exps.retired");
  const rows = (data ?? []).filter((e) => retired.keep(e) && e.name.toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <PageTitle title="Experiments" />
      <Spacer size="md" />
      <div css={{ display: "flex", gap: theme.spacing.sm, alignItems: "center" }}>
        <div css={{ width: 358 }}>
        <Input componentId="exps.filter" prefix={<SearchIcon />} placeholder="Filter experiments by name" value={q}
               onChange={(e) => setQ(e.target.value)} allowClear />
        </div>
        {retired.toggle}
      </div>
      <Spacer size="md" />
      {error && <Alert componentId="exps.error" type="error" message="Couldn't load experiments" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={6} />}
      {data && (
        <Table scrollable empty={rows.length ? undefined : <Empty title="No experiments" description={q ? "Try a different name." : "Experiments appear here once training logs to Vertex AI."} />}>
          <TableRow isHeader>
            <TableHeader componentId="exps.h.name">Name</TableHeader>
            <TableHeader componentId="exps.h.modified">Last modified</TableHeader>
            <TableHeader componentId="exps.h.created">Created</TableHeader>
            <TableHeader componentId="exps.h.location">Location</TableHeader>
            <TableHeader componentId="exps.h.desc">Description</TableHeader>
          </TableRow>
          {rows.map((e) => (
            <TableRow key={e.id}>
              <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.xs }}><Link to={`/experiments/${e.id}`}>{e.name}</Link><RetiredTag retired={e.retired} /></span></TableCell>
              <TableCell>{when(e.updated, true)}</TableCell>
              <TableCell>{when(e.created, true)}</TableCell>
              <TableCell><Typography.Text color="secondary">Vertex AI Experiments</Typography.Text></TableCell>
              <TableCell>{e.description}</TableCell>
            </TableRow>
          ))}
        </Table>
      )}
    </>
  );
}
