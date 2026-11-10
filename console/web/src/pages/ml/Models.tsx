import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Alert, Empty, Input, SearchIcon, Spacer, TableCell, TableRow, TableSkeleton, Tag, Typography,
  useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../../components/Table";
import { api } from "../../api";
import PageTitle from "../../components/PageTitle";
import { RetiredTag, useShowRetired } from "../../components/Retired";
import { ago, useLoad, when } from "../../util";

export default function ModelsPage() {
  const { theme } = useDesignSystemTheme();
  const { data, error } = useLoad(api.models, []);
  const [q, setQ] = useState("");
  const retired = useShowRetired("models.retired");
  const rows = (data ?? []).filter((m) => retired.keep(m) && (m.name ?? "").toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <PageTitle title="Models" />
      <Spacer size="md" />
      <div css={{ display: "flex", gap: theme.spacing.sm, alignItems: "center" }}>
        <div css={{ width: 420 }}>
        <Input componentId="models.filter" prefix={<SearchIcon />} placeholder="Filter models by name" value={q}
               onChange={(e) => setQ(e.target.value)} allowClear />
        </div>
        {retired.toggle}
      </div>
      <Spacer size="md" />
      {error && <Alert componentId="models.error" type="error" message="Couldn't load models" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={8} />}
      {data && (
        <Table scrollable empty={rows.length ? undefined : <Empty title="No models" description={q ? "Try a different name." : "Models appear here once a version is registered in Vertex AI."} />}>
          <TableRow isHeader>
            <TableHeader componentId="models.h.name">Name</TableHeader>
            <TableHeader componentId="models.h.latest">Latest version</TableHeader>
            <TableHeader componentId="models.h.versions" align="right" css={{ flex: "0 0 90px" }}>Versions</TableHeader>
            <TableHeader componentId="models.h.aliases">Aliases</TableHeader>
            <TableHeader componentId="models.h.modified">Last modified</TableHeader>
            <TableHeader componentId="models.h.desc">Description</TableHeader>
          </TableRow>
          {rows.map((m) => (
            <TableRow key={m.id}>
              <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.xs }}><Link to={`/models/${m.id}`}>{m.name}</Link><RetiredTag retired={m.retired} /></span></TableCell>
              <TableCell>{m.latest ? <Link to={`/models/${m.id}/versions/${m.latest}`}>Version {m.latest}</Link> : "-"}</TableCell>
              <TableCell css={{ flex: "0 0 90px", justifyContent: "flex-end" }}>{m.versions}</TableCell>
              <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.xs, flexWrap: "wrap" }}>
                {Object.entries(m.aliases).map(([a, v]) => <Link key={a} to={`/models/${m.id}/versions/${v}`}>
                  <Tag componentId="models.alias" color="indigo">@{a}: Version {v}</Tag></Link>)}
                {Object.keys(m.aliases).length === 0 && <Typography.Text color="secondary">-</Typography.Text>}</span></TableCell>
              <TableCell><span title={when(m.updated, true)}>{ago(m.updated)}</span></TableCell>
              <TableCell>{m.description}</TableCell>
            </TableRow>
          ))}
        </Table>
      )}
    </>
  );
}
