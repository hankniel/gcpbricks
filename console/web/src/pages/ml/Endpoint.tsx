import { useParams } from "react-router-dom";
import {
  Alert, CloudModelIcon, Spacer, TableCell, TableRow, TableSkeleton, Tag, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../../components/Table";
import { api } from "../../api";
import { Field, Section } from "../../components/Panel";
import PageTitle from "../../components/PageTitle";
import SidePanel from "../../components/SidePanel";
import { RetiredNotice } from "../../components/Retired";
import { Status } from "../../components/Status";
import { useLoad, when } from "../../util";

export default function EndpointPage() {
  const { id = "" } = useParams();
  const { theme } = useDesignSystemTheme();
  const { data: e, error } = useLoad(() => api.endpoint(id), [id], 30000);
  return (
    <>
      <PageTitle crumbs={[{ to: "/serving", label: "Serving endpoints" }]} icon={<CloudModelIcon />} title={e?.name ?? id} />
      <Spacer size="md" />
      {error && <Alert componentId="endpoint.error" type="error" message="Couldn't load this endpoint" description={error} closable={false} />}
      {!e && !error && <TableSkeleton lines={5} />}
      {e && <RetiredNotice kind="endpoint" name={e.name} retired={e.retired} remove={() => api.deleteEndpoint(e.id)} back="/serving" />}
      {e && (
        <SidePanel>
          <div>
            <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.md, padding: theme.spacing.md,
                        border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd }}>
              <Status state={e.ready ? "SUCCESS" : "PENDING"} />
              <Typography.Text color="secondary">Vertex AI endpoint {e.id}</Typography.Text>
            </div>
            <Spacer size="lg" />
            <Typography.Title level={3}>Active configuration</Typography.Title>
            <Table scrollable>
              <TableRow isHeader>
                <TableHeader componentId="endpoint.h.entity">Entity</TableHeader>
                <TableHeader componentId="endpoint.h.version">Version</TableHeader>
                <TableHeader componentId="endpoint.h.name">Name</TableHeader>
                <TableHeader componentId="endpoint.h.compute">Compute</TableHeader>
                <TableHeader componentId="endpoint.h.traffic" align="right">Traffic (%)</TableHeader>
              </TableRow>
              {e.deployed.map((d) => (
                <TableRow key={d.id}>
                  <TableCell>{d.model}</TableCell>
                  <TableCell>{d.version}</TableCell>
                  <TableCell>{d.name}</TableCell>
                  <TableCell><Typography.Text bold>{d.machine || "-"}</Typography.Text> {d.replicas[0] != null ? `${d.replicas[0]}-${d.replicas[1]} replicas` : ""}</TableCell>
                  <TableCell align="right">{d.traffic}</TableCell>
                </TableRow>
              ))}
            </Table>
          </div>
          <div>
            <Section title="About this endpoint">
              <Field name="Created">{when(e.created, true)}</Field>
              <Field name="Last modified">{when(e.updated, true)}</Field>
            </Section>
            <Section title="Tags">
              {Object.keys(e.labels).length === 0 ? <Typography.Text color="secondary">No tags</Typography.Text>
                : Object.entries(e.labels).map(([k, v]) => <div key={k}><Tag componentId="endpoint.tag">{k}: {v}</Tag></div>)}
            </Section>
            <Section title="Description">
              <Typography.Text color="secondary">{e.description || "No description"}</Typography.Text>
            </Section>
          </div>
        </SidePanel>
      )}
    </>
  );
}
