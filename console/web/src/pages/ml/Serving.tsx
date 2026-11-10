import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Alert, CloudModelIcon, Empty, Input, SearchIcon, Spacer, TableCell, TableRow, TableSkeleton, Tag,
  Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../../components/Table";
import { api } from "../../api";
import PageTitle from "../../components/PageTitle";
import { RetiredTag, useShowRetired } from "../../components/Retired";
import { Status } from "../../components/Status";
import { ago, useLoad } from "../../util";

export default function ServingPage() {
  const { theme } = useDesignSystemTheme();
  const { data, error } = useLoad(api.endpoints, [], 30000);
  const [q, setQ] = useState("");
  const retired = useShowRetired("serving.retired");
  const rows = (data ?? []).filter((e) => retired.keep(e) && (e.name ?? "").toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <PageTitle title="Serving endpoints" />
      <Spacer size="md" />
      <div css={{ display: "flex", gap: theme.spacing.sm, alignItems: "center" }}>
        <div css={{ width: 353 }}>
        <Input componentId="serving.filter" prefix={<SearchIcon />} placeholder="Filter serving endpoints by name" value={q}
               onChange={(e) => setQ(e.target.value)} allowClear />
        </div>
        {retired.toggle}
      </div>
      <Spacer size="md" />
      {error && <Alert componentId="serving.error" type="error" message="Couldn't load serving endpoints" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={4} />}
      {data && (
        <Table scrollable empty={rows.length ? undefined : (
          <Empty title="No serving endpoints" description="Declare one under model_serving_endpoints in a bundle's mlops.yaml; deploy creates it on Vertex AI." />
        )}>
          <TableRow isHeader>
            <TableHeader componentId="serving.h.name">Name</TableHeader>
            <TableHeader componentId="serving.h.state">State</TableHeader>
            <TableHeader componentId="serving.h.served">Served entities</TableHeader>
            <TableHeader componentId="serving.h.tags">Tags</TableHeader>
            <TableHeader componentId="serving.h.modified">Last modified</TableHeader>
          </TableRow>
          {rows.map((e) => (
            <TableRow key={e.id}>
              <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.xs, alignItems: "center" }}><CloudModelIcon />
                <Link to={`/serving/${e.id}`}>{e.name}</Link><RetiredTag retired={e.retired} /></span></TableCell>
              <TableCell><Status state={e.ready ? "SUCCESS" : "PENDING"} /></TableCell>
              <TableCell>{e.deployed.length ? e.deployed.map((d) => `${d.model} v${d.version}`).join(", ") : <Typography.Text color="secondary">No served entities</Typography.Text>}</TableCell>
              <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.xs }}>
                {Object.entries(e.labels).map(([k, v]) => <Tag key={k} componentId="serving.tag">{k}: {v}</Tag>)}</span></TableCell>
              <TableCell>{ago(e.updated)}</TableCell>
            </TableRow>
          ))}
        </Table>
      )}
    </>
  );
}
