import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Alert, Empty, Input, SearchIcon, SegmentedControlButton, SegmentedControlGroup, Spacer, TableCell, TableIcon, TableRow,
  TableSkeleton, Tabs, Tag, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../../components/Table";
import { api, type FeatureGroup, type FeatureGroupDetail } from "../../api";
import { Field, Section } from "../../components/Panel";
import PageTitle from "../../components/PageTitle";
import SidePanel from "../../components/SidePanel";
import SampleGrid from "../../components/SampleGrid";
import { useLoad, when } from "../../util";

function size(b: number | null): string {
  if (b == null) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = b;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}

/** The keys a group's rows are identified and joined by, and its timestamp: each with its type
 *  as declared and as the table has it, and the other groups sharing it, which join on it. */
function Entities({ g }: { g: FeatureGroupDetail }) {
  const { theme } = useDesignSystemTheme();
  const groups = useLoad(api.features, []);
  const rows = [...g.keys.map((k) => ({ name: k, role: "Primary key", type: g.key_types[k] ?? "-" })),
                { name: g.timestamp, role: "Timestamp", type: "UnixTimestamp" }];
  return (
    <>
      <Typography.Paragraph color="secondary">
        One row per {g.keys.join(" × ")}, as of {g.timestamp}. A training set joins this group to others on these keys, point in time.
      </Typography.Paragraph>
      <Table scrollable>
        <TableRow isHeader>
          <TableHeader componentId="fg.e.name" css={{ flex: "0 0 260px" }}>Entity</TableHeader>
          <TableHeader componentId="fg.e.role" css={{ flex: "0 0 120px" }}>Role</TableHeader>
          <TableHeader componentId="fg.e.type" css={{ flex: "0 0 130px" }}>Declared type</TableHeader>
          <TableHeader componentId="fg.e.live" css={{ flex: "0 0 110px" }}>In the table</TableHeader>
          <TableHeader componentId="fg.e.shared">Also keyed on it</TableHeader>
        </TableRow>
        {rows.map((r) => {
          const shared = (groups.data ?? []).filter((o) => o.name !== g.name && (r.role === "Timestamp" ? o.timestamp === r.name : o.keys.includes(r.name)));
          return (
            <TableRow key={r.name}>
              <TableCell css={{ flex: "0 0 260px" }}><Typography.Text bold code>{r.name}</Typography.Text></TableCell>
              <TableCell css={{ flex: "0 0 120px" }}><Tag componentId="fg.e.roletag" color={r.role === "Timestamp" ? "turquoise" : "indigo"}>{r.role}</Tag></TableCell>
              <TableCell css={{ flex: "0 0 130px" }}>{r.type}</TableCell>
              <TableCell css={{ flex: "0 0 110px" }}>{g.live ? (g.live.types[r.name] ?? <Typography.Text color="error">missing</Typography.Text>) : "-"}</TableCell>
              <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.xs, flexWrap: "wrap" }}>
                {groups.data ? (shared.length ? shared.map((o) => <Link key={o.name} to={`/features/${o.name}`}>
                  <Tag componentId="fg.e.group">{o.name}</Tag></Link>) : <Typography.Text color="secondary">No other group</Typography.Text>) : "..."}
              </span></TableCell>
            </TableRow>
          );
        })}
      </Table>
    </>
  );
}

/** The group's table's first rows, through Catalog's free preview (tabledata.list). */
function Sample({ dataset, table, project, types }: {
  dataset: string; table: string; project: string; types: Record<string, { type: string }>;
}) {
  const { data, error } = useLoad(() => api.preview(dataset, table, project), [dataset, table, project]);
  if (error) return <Alert componentId="fg.sample.error" type="error" message="Couldn't read a sample" description={error} closable={false} />;
  if (!data) return <TableSkeleton lines={6} />;
  return <SampleGrid sample={data} types={types} />;
}

/** One kind's groups: a label table's columns are training targets, a feature table's model inputs. */
function GroupTable({ rows, label }: { rows: FeatureGroup[]; label: boolean }) {
  const noun = label ? "Label" : "Feature";
  return (
    <Table scrollable empty={rows.length ? undefined : <Empty title={`No ${noun.toLowerCase()} tables`}
      description={label ? "A group is a label table once dbt-features declares meta.feature_store.kind: label."
        : "Feature groups appear once dbt-features declares meta.feature_store and its CI publishes them."} />}>
      <TableRow isHeader>
        <TableHeader componentId="features.h.name" css={{ flex: "1 1 200px" }}>Name</TableHeader>
        <TableHeader componentId="features.h.table" css={{ flex: "1 1 240px" }}>{noun} table</TableHeader>
        <TableHeader componentId="features.h.desc" css={{ flex: "2 1 300px" }}>Description</TableHeader>
        <TableHeader componentId="features.h.features">{label ? "Labels" : "Features"}</TableHeader>
        <TableHeader componentId="features.h.keys">Keys</TableHeader>
        <TableHeader componentId="features.h.source">Data source</TableHeader>
        <TableHeader componentId="features.h.project">dbt project</TableHeader>
      </TableRow>
      {rows.map((g) => (
        <TableRow key={g.name}>
          <TableCell css={{ flex: "1 1 200px" }}><Link to={`/features/${g.name}`}>{g.display_name || g.name}</Link></TableCell>
          <TableCell css={{ flex: "1 1 240px" }}><Typography.Text ellipsis>{g.name}</Typography.Text></TableCell>
          <TableCell css={{ flex: "2 1 300px" }}><Typography.Text ellipsis color="secondary" title={g.description}>{g.description || "-"}</Typography.Text></TableCell>
          <TableCell><Typography.Text ellipsis>{g.features.map((f) => f.name).join(", ")}</Typography.Text></TableCell>
          <TableCell><Typography.Text ellipsis>{g.keys.join(", ")}</Typography.Text></TableCell>
          <TableCell><Typography.Text ellipsis>{g.offline_table}</Typography.Text></TableCell>
          <TableCell>{g.project}</TableCell>
        </TableRow>
      ))}
    </Table>
  );
}

export function FeaturesPage() {
  const { data, error } = useLoad(api.features, []);
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("features");
  const ql = q.toLowerCase();
  const rows = (data ?? []).filter((g) => [g.name, g.display_name ?? "", g.description, g.offline_table]
    .some((v) => v.toLowerCase().includes(ql)) || g.features.some((f) => f.name.toLowerCase().includes(ql)));
  // A config published before `kind` has none: every group is a feature table.
  const labels = rows.filter((g) => g.kind === "label");
  const features = rows.filter((g) => g.kind !== "label");
  return (
    <>
      <PageTitle title="Feature Store" />
      <Spacer size="md" />
      {/* Databricks' Features page: the kind of table as a switch under the title, then the filter. */}
      <SegmentedControlGroup componentId="features.kind" name="kind" value={kind} onChange={(e) => setKind(e.target.value)}>
        <SegmentedControlButton value="features">Feature tables ({features.length})</SegmentedControlButton>
        <SegmentedControlButton value="labels">Label tables ({labels.length})</SegmentedControlButton>
      </SegmentedControlGroup>
      <Spacer size="md" />
      <div css={{ width: 353 }}>
        <Input componentId="features.filter" prefix={<SearchIcon />} placeholder="Filter by name, description, table, feature or source" value={q}
               onChange={(e) => setQ(e.target.value)} allowClear />
      </div>
      <Spacer size="md" />
      {error && <Alert componentId="features.error" type="error" message="Couldn't load feature groups" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={4} />}
      {data && <GroupTable rows={kind === "labels" ? labels : features} label={kind === "labels"} />}
    </>
  );
}

export function FeatureGroupPage() {
  const { name = "" } = useParams();
  const { theme } = useDesignSystemTheme();
  const { data: g, error } = useLoad(() => api.featureGroup(name), [name]);
  const [q, setQ] = useState("");
  const feats = (g?.features ?? []).filter((f) => f.name.toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <PageTitle crumbs={[{ to: "/features", label: "Feature Store" }]} icon={<TableIcon />} title={g?.display_name || name}
                 addOns={g?.kind === "label" && <Tag componentId="fg.kind" color="lemon">Label table</Tag>} />
      {g?.description && <Typography.Paragraph color="secondary" css={{ "&&": { marginTop: theme.spacing.sm, marginBottom: 0 } }}>{g.description}</Typography.Paragraph>}
      <Spacer size="md" />
      {error && <Alert componentId="fg.error" type="error" message="Couldn't load feature groups" description={error} closable={false} />}
      {!g && !error && <TableSkeleton lines={8} />}
      {g && g.declared_not_live.length > 0 && <><Alert componentId="fg.missing" type="error" closable={false}
        message={`${g.declared_not_live.length} declared column(s) missing from the table`}
        description={`Reading them fails: ${g.declared_not_live.join(", ")}`} /><Spacer size="sm" /></>}
      {g && g.live_not_declared.length > 0 && <><Alert componentId="fg.undeclared" type="warning" closable={false}
        message={`${g.live_not_declared.length} column(s) in the table aren't declared`}
        description={`The SDK serves only declared columns: ${g.live_not_declared.slice(0, 20).join(", ")}${g.live_not_declared.length > 20 ? ", ..." : ""}`} /><Spacer size="sm" /></>}
      {g && (
        <SidePanel>
          <Tabs.Root componentId="fg.tabs" defaultValue="features">
            <Tabs.List>
              <Tabs.Trigger value="features">{g.kind === "label" ? "Labels" : "Features"}</Tabs.Trigger>
              <Tabs.Trigger value="entities">Entities</Tabs.Trigger>
              <Tabs.Trigger value="sample" disabled={!g.live?.previewable}>Sample data</Tabs.Trigger>
            </Tabs.List>
            <Tabs.Content value="entities">
              <Entities g={g} />
            </Tabs.Content>
            <Tabs.Content value="sample">
              {g.live && <Sample dataset={g.live.dataset} table={g.live.id} project={g.live.project}
                types={Object.fromEntries(Object.entries(g.live.types).map(([c, t]) => [c, { type: t }]))} />}
            </Tabs.Content>
            <Tabs.Content value="features">
            <div css={{ width: 360 }}>
              <Input componentId="fg.filter" prefix={<SearchIcon />} placeholder={g.kind === "label" ? "Filter labels" : "Filter features"} value={q}
                     onChange={(e) => setQ(e.target.value)} allowClear />
            </div>
            <Spacer size="md" />
            <Typography.Text bold>{g.kind === "label" ? "Labels" : "Features"} ({g.n_features})</Typography.Text>
            <Spacer size="sm" />
            <Table scrollable>
              <TableRow isHeader>
                <TableHeader componentId="fg.h.feature">{g.kind === "label" ? "Label" : "Feature"}</TableHeader>
                <TableHeader componentId="fg.h.type">Data Type</TableHeader>
                <TableHeader componentId="fg.h.live">In the table</TableHeader>
                <TableHeader componentId="fg.h.family">Family</TableHeader>
                <TableHeader componentId="fg.h.desc">Description</TableHeader>
              </TableRow>
              {feats.map((f) => (
                <TableRow key={f.name}>
                  <TableCell>{f.name}</TableCell>
                  <TableCell>{f.dtype.toUpperCase()}</TableCell>
                  <TableCell>{g.live ? (g.live.types[f.name] ?? <Typography.Text color="error">missing</Typography.Text>) : "-"}</TableCell>
                  <TableCell><Tag componentId="fg.family">{f.family}</Tag></TableCell>
                  <TableCell><Typography.Text ellipsis color="secondary">{f.description}</Typography.Text></TableCell>
                </TableRow>
              ))}
            </Table>
            </Tabs.Content>
          </Tabs.Root>
          <div>
            <Section title="Offline table">
              {g.live ? <>
                <Field name="Table"><Link to={`/catalog/${g.live.dataset}/${g.live.id}${g.live.project ? `?project=${g.live.project}` : ""}`}>{g.live.full_name}</Link></Field>
                <Field name="Rows">{g.live.rows?.toLocaleString("en-US") ?? "-"}</Field>
                <Field name="Size">{size(g.live.bytes)}</Field>
                <Field name="Last modified">{when(g.live.updated, true)}</Field>
                <Field name="Columns">{Object.keys(g.live.types).length}</Field>
              </> : <Typography.Text color="error">{g.live_error}</Typography.Text>}
            </Section>
            <Section title="Feature table details">
              <Field name="Name">{g.display_name || "-"}</Field>
              <Field name="Feature table">{g.name}</Field>
              <Field name="Data source">{g.offline_table}</Field>
              <Field name="Keys">{g.keys.join(", ")}</Field>
              <Field name="Timestamp">{g.timestamp}</Field>
              <Field name="dbt project">{g.project}</Field>
            </Section>
            <Section title="Families">
              {Object.entries(g.families).map(([k, v]) => <Field key={k} name={k}>{v} features</Field>)}
            </Section>
          </div>
        </SidePanel>
      )}
    </>
  );
}
