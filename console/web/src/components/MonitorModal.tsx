import { useEffect, useMemo, useState } from "react";
import {
  Alert, Button, Checkbox, ChevronDownIcon, ChevronRightIcon, CloseSmallIcon, CopyIcon, DropdownMenu, FormUI, InfoIcon,
  Input, Modal, PlusIcon, Radio, SimpleSelect, SimpleSelectOption, Spacer, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { api, Column, QualityMonitor } from "../api";

type ProfileType = "time_series" | "snapshot" | "inference_log";
type Freq = "hour" | "day" | "week" | "custom";
type Metric = { type: string; name: string; input_columns: string; definition: string; output_data_type: string };

const PROFILE_TYPES: [ProfileType, string, string][] = [
  ["time_series", "Time series", "Quality metrics aggregated over time windows"],
  ["snapshot", "Snapshot", "Quality metrics calculated over the full table"],
  ["inference_log", "Inference", "Model drift and performance over time"],
];
const GRANULARITIES = ["5 minutes", "30 minutes", "1 hour", "1 day", "1 week", "2 weeks", "3 weeks", "4 weeks", "1 month", "1 year"];
const DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"];
const METRIC_TYPES: [string, string][] = [["CUSTOM_METRIC_TYPE_AGGREGATE", "Aggregate"], ["CUSTOM_METRIC_TYPE_DERIVED", "Derived"],
  ["CUSTOM_METRIC_TYPE_DRIFT", "Drift"]];
const OUTPUT_TYPES = ["double", "long", "string", "boolean"];
const NONE = "\u0001none";
const EMAIL = /^[^@\s/]+@[^@\s/]+\.[^@\s/]+$/;

/** A quartz cron the schedule picker can show: "0 M H * * ?" daily, "0 M H ? * DAY" weekly, "0 0 * * * ?" hourly. */
function parseCron(cron: string | undefined): { freq: Freq; hour: string; minute: string; day: string } {
  const base = { hour: "6", minute: "0", day: "MON" };
  if (!cron) return { freq: "day", ...base };
  const p = cron.trim().split(/\s+/);
  if (p.length === 6 && p[0] === "0" && p[1] === "0" && p[2] === "*" && p[3] === "*" && p[4] === "*" && p[5] === "?")
    return { freq: "hour", ...base };
  if (p.length === 6 && p[0] === "0" && /^\d+$/.test(p[1]) && /^\d+$/.test(p[2]) && p[4] === "*") {
    if (p[3] === "*" && p[5] === "?") return { freq: "day", hour: p[2], minute: p[1], day: "MON" };
    if (p[3] === "?" && DAYS.includes(p[5])) return { freq: "week", hour: p[2], minute: p[1], day: p[5] };
  }
  return { freq: "custom", ...base };
}

function toCron(freq: Freq, hour: string, minute: string, day: string, custom: string): string {
  if (freq === "hour") return "0 0 * * * ?";
  if (freq === "day") return `0 ${Number(minute)} ${Number(hour)} * * ?`;
  if (freq === "week") return `0 ${Number(minute)} ${Number(hour)} ? * ${day}`;
  return custom.trim();
}

/** Databricks' "Create monitor" and "Update profile" dialogs: the profile type and its columns,
 *  then Advanced options (schedule, notifications, metrics schema, baseline, slicing expressions,
 *  custom metrics). The monitor is shown as the bundle's YAML; `deploy`, when given, creates or
 *  updates it directly, and `remove` deletes it (Update's menu). */
export default function MonitorModal({ open, onClose, table, columns, monitor, deploy, remove, targets = [] }: {
  open: boolean; onClose: () => void;
  /** project.dataset.table */
  table: string; columns: Column[];
  /** Given: the Update dialog for it. */
  monitor?: QualityMonitor;
  /** Creates or updates it in the console's own bundle on `target`. */
  deploy?: (key: string, spec: Record<string, unknown>, target: string) => Promise<void>;
  remove?: () => Promise<void>;
  /** Where Create can deploy (a new monitor's Target picker). */
  targets?: string[];
}) {
  const { theme } = useDesignSystemTheme();
  const cfg = (monitor?.config ?? {}) as Record<string, any>;
  const [project, dataset, tableName] = table.split(".");
  const [key, setKey] = useState("");
  const [target, setTarget] = useState("");
  const [type, setType] = useState<ProfileType | "">("");
  const [problem, setProblem] = useState("PROBLEM_TYPE_REGRESSION");
  const [cols, setCols] = useState<Record<string, string>>({});
  const [grans, setGrans] = useState<string[]>(["1 day"]);
  const [advanced, setAdvanced] = useState(false);
  const [scheduled, setScheduled] = useState(false);
  const [freq, setFreq] = useState<Freq>("day");
  const [hour, setHour] = useState("6");
  const [minute, setMinute] = useState("0");
  const [day, setDay] = useState("MON");
  const [customCron, setCustomCron] = useState("");
  const [tz, setTz] = useState("UTC");
  const [emails, setEmails] = useState("");
  const [schema, setSchema] = useState("");
  const [baseline, setBaseline] = useState("");
  const [slices, setSlices] = useState<string[]>([]);
  const [lookback, setLookback] = useState("");
  const [metrics, setMetrics] = useState<Metric[]>([]);
  const [yaml, setYaml] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    if (!open) return;
    const kind = (["inference_log", "time_series", "snapshot"] as ProfileType[]).find((k) => cfg[k] != null) ?? "";
    const p = (kind && cfg[kind]) || {};
    setTarget(monitor?.target ?? (targets.includes("dev") ? "dev" : targets[0] ?? ""));
    setKey(monitor?.key ?? tableName.toLowerCase().replace(/[^a-z0-9_-]/g, "_").replace(/^[^a-z]+/, "").slice(0, 32));
    setType(kind);
    setProblem(p.problem_type ?? "PROBLEM_TYPE_REGRESSION");
    setCols({ timestamp_col: p.timestamp_col ?? "", prediction_col: p.prediction_col ?? "", label_col: p.label_col ?? "",
              model_id_col: p.model_id_col ?? "", prediction_proba_col: p.prediction_proba_col ?? "" });
    setGrans(p.granularities ?? ["1 day"]);
    const s = parseCron(cfg.schedule?.quartz_cron_expression);
    setScheduled(!!cfg.schedule); setFreq(s.freq); setHour(s.hour); setMinute(s.minute); setDay(s.day);
    setCustomCron(cfg.schedule?.quartz_cron_expression ?? ""); setTz(cfg.schedule?.timezone_id ?? "UTC");
    setEmails((cfg.notifications?.on_failure?.email_addresses ?? []).join(", "));
    setSchema(cfg.output_schema_name ?? `${project}.${dataset}`);
    setBaseline(cfg.baseline_table_name ?? "");
    setSlices(cfg.slicing_exprs ?? []);
    setLookback(cfg.lookback_days != null ? String(cfg.lookback_days) : "");
    setMetrics((cfg.custom_metrics ?? []).map((c: any) => ({ ...c, input_columns: (c.input_columns ?? []).join(", ") })));
    setAdvanced(false); setYaml(null); setError(null); setBusy(false); setConfirmDelete(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, monitor, table]);

  const emailList = emails.split(/[\s,]+/).filter(Boolean);
  const spec = useMemo(() => {
    if (!type) return null;
    const pick = (k: string) => cols[k] || undefined;
    const profile: Record<string, unknown> =
      type === "snapshot" ? {}
      : type === "time_series" ? { timestamp_col: pick("timestamp_col"), granularities: grans }
      : { problem_type: problem, prediction_col: pick("prediction_col"), label_col: pick("label_col"),
          model_id_col: pick("model_id_col"), timestamp_col: pick("timestamp_col"), granularities: grans,
          ...(problem === "PROBLEM_TYPE_CLASSIFICATION" ? { prediction_proba_col: pick("prediction_proba_col") } : {}) };
    const out: Record<string, unknown> = {
      // What the dialog doesn't edit (anomaly_detection, assets_dir) carries over.
      ...Object.fromEntries(Object.entries(cfg).filter(([k]) => !["inference_log", "time_series", "snapshot"].includes(k))),
      table_name: table, output_schema_name: schema.trim(), [type]: JSON.parse(JSON.stringify(profile)),
      baseline_table_name: baseline.trim() || undefined,
      slicing_exprs: slices.map((s) => s.trim()).filter(Boolean),
      custom_metrics: metrics.map((c) => ({ ...c, input_columns: c.input_columns.split(/[\s,]+/).filter(Boolean) })),
      schedule: scheduled ? { quartz_cron_expression: toCron(freq, hour, minute, day, customCron), timezone_id: tz.trim() || "UTC" } : undefined,
      notifications: emailList.length ? { on_failure: { email_addresses: emailList } } : undefined,
      lookback_days: type !== "snapshot" && lookback.trim() ? Number(lookback) : undefined,
    };
    if (!(out.slicing_exprs as string[]).length) delete out.slicing_exprs;
    if (!(out.custom_metrics as unknown[]).length) delete out.custom_metrics;
    return JSON.parse(JSON.stringify(out)) as Record<string, unknown>;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [type, problem, cols, grans, schema, baseline, slices, metrics, scheduled, freq, hour, minute, day, customCron, tz, emails, lookback, table]);

  const missing = deploy && !target ? "No target to deploy to"
    : !type ? "Select a profile type"
    : type !== "snapshot" && !cols.timestamp_col ? "Select the timestamp column"
    : type !== "snapshot" && !grans.length ? "Pick at least one metric granularity"
    : type === "inference_log" && (!cols.prediction_col || !cols.model_id_col) ? "Select the prediction and model ID columns"
    : emailList.some((e) => !EMAIL.test(e)) ? "An email address isn't valid"
    : emailList.length > 5 ? "Up to 5 emails are supported per notification event type"
    : null;

  const showYaml = async () => {
    if (!spec) return;
    setBusy(true); setError(null);
    try { setYaml((await api.monitorYaml(key, spec)).yaml); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const submit = async () => {
    if (!spec) return;
    if (!deploy) { await showYaml(); return; }
    setBusy(true); setError(null);
    try { await api.monitorYaml(key, spec); await deploy(key, spec, target); onClose(); }
    catch (e) { setError((e as Error).message); setBusy(false); }
  };
  const doRemove = async () => {
    if (!remove) return;
    setBusy(true); setError(null);
    try { await remove(); onClose(); } catch (e) { setError((e as Error).message); setBusy(false); }
  };

  const colSelect = (k: string, label: string, optional = false, hint?: string) => (
    <div key={k}>
      <FormUI.Label htmlFor={`mon-${k}`}>{label}{optional && <Typography.Text color="secondary"> (optional)</Typography.Text>}</FormUI.Label>
      {hint && <FormUI.Hint>{hint}</FormUI.Hint>}
      <SimpleSelect componentId={`quality.create.${k}`} id={`mon-${k}`} placeholder="Select column" width="100%"
                    value={cols[k] || (optional ? NONE : "")}
                    onChange={(e) => setCols({ ...cols, [k]: e.target.value === NONE ? "" : e.target.value })}>
        {optional && <SimpleSelectOption value={NONE}>None</SimpleSelectOption>}
        {cols[k] && !columns.some((c) => c.name === cols[k]) &&
          <SimpleSelectOption value={cols[k]}>{`${cols[k]} · not in the table's schema`}</SimpleSelectOption>}
        {columns.map((c) => <SimpleSelectOption key={c.name} value={c.name}>{`${c.name} · ${c.type}`}</SimpleSelectOption>)}
      </SimpleSelect>
    </div>
  );
  const section = (title: string, body: React.ReactNode) => (
    <div css={{ display: "grid", gridTemplateColumns: "180px minmax(0, 1fr)", gap: theme.spacing.lg, padding: `${theme.spacing.md}px 0` }}>
      <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>{title}</Typography.Title>
      <div css={{ display: "flex", flexDirection: "column", gap: theme.spacing.md, minWidth: 0 }}>{body}</div>
    </div>
  );
  const updating = !!monitor;
  const primary = updating ? (deploy ? "Update" : "Get updated YAML") : deploy ? "Create" : "Get YAML";

  const footer = (
    <div css={{ display: "flex", justifyContent: "space-between", gap: theme.spacing.sm }}>
      <Button componentId="quality.create.yaml" icon={<CopyIcon />} onClick={showYaml} disabled={!!missing || busy}>View YAML</Button>
      <div css={{ display: "flex", gap: theme.spacing.sm }}>
        <Button componentId="quality.create.cancel" onClick={onClose}>Cancel</Button>
        {updating ? (
          <div css={{ display: "inline-flex" }}>
            <Button componentId="quality.update.ok" type="primary" onClick={submit} loading={busy} disabled={!!missing}
                    css={{ borderTopRightRadius: 0, borderBottomRightRadius: 0 }}>{primary}</Button>
            <DropdownMenu.Root modal={false}>
              <DropdownMenu.Trigger asChild>
                <Button componentId="quality.update.more" type="primary" icon={<ChevronDownIcon />} aria-label="More update actions"
                        css={{ borderTopLeftRadius: 0, borderBottomLeftRadius: 0, marginLeft: 1 }} />
              </DropdownMenu.Trigger>
              <DropdownMenu.Content align="end">
                <DropdownMenu.Item componentId="quality.update.delete" danger onClick={() => setConfirmDelete(true)}>Delete</DropdownMenu.Item>
              </DropdownMenu.Content>
            </DropdownMenu.Root>
          </div>
        ) : (
          <Button componentId="quality.create.ok" type="primary" onClick={submit} loading={busy} disabled={!!missing}>{primary}</Button>
        )}
      </div>
    </div>
  );

  return (
    <Modal componentId="quality.create.modal" title={updating ? "Update profile" : "Create monitor"} visible={open} onCancel={onClose}
           size="wide" footer={footer}>
      <Alert componentId="quality.create.info" type="info" closable={false}
             message={<>{updating ? "Update the monitor" : "Create a monitor"} to capture profile and drift metrics for <Typography.Text bold>{table}</Typography.Text>.</>} />
      {error && <><Spacer size="sm" /><Alert componentId="quality.create.error" type="error" message={error} closable={false} /></>}
      {confirmDelete && (
        <>
          <Spacer size="sm" />
          <Alert componentId="quality.update.delete.confirm" type="warning" closable={false} message={`Delete monitor ${monitor?.key}?`}
                 description={remove
                   ? <>Its refresh job stops and is removed. The metric tables stay: delete them yourself if you no longer need them.{" "}
                       <Button componentId="quality.update.delete.ok" danger size="small" loading={busy} onClick={doRemove}>Delete</Button></>
                   : <>This monitor is declared in bundle <Typography.Text code>{monitor?.bundle}</Typography.Text>: remove{" "}
                       <Typography.Text code>{`resources.quality_monitors.${monitor?.key}`}</Typography.Text> from it and deploy. Its job is
                       then retired, and the metric tables stay.</>} />
        </>
      )}
      {section("Profiling", <>
        {deploy && (
          <div css={{ display: "grid", gridTemplateColumns: "1fr 160px", gap: theme.spacing.sm }}>
            <div>
              <FormUI.Label htmlFor="mon-key">Monitor name</FormUI.Label>
              <FormUI.Hint>Its refresh job is {key || "<name>"}-monitor.</FormUI.Hint>
              <Input componentId="quality.create.key" id="mon-key" value={key} disabled={updating}
                     onChange={(e) => setKey(e.target.value.toLowerCase())} />
            </div>
            <div>
              <FormUI.Label htmlFor="mon-target">Target</FormUI.Label>
              <FormUI.Hint>&nbsp;</FormUI.Hint>
              <SimpleSelect componentId="quality.create.target" id="mon-target" value={target} width="100%" disabled={updating}
                            onChange={(e) => setTarget(e.target.value)}>
                {(updating ? [target] : targets).map((t) => <SimpleSelectOption key={t} value={t}>{t}</SimpleSelectOption>)}
              </SimpleSelect>
            </div>
          </div>
        )}
        <div>
          <FormUI.Label htmlFor="mon-type">Profile type</FormUI.Label>
          <SimpleSelect componentId="quality.create.type" id="mon-type" placeholder="Select type" width="100%" value={type}
                        onChange={(e) => setType(e.target.value as ProfileType)}>
            {PROFILE_TYPES.map(([v, label, hint]) => (
              <SimpleSelectOption key={v} value={v}>
                <div><div>{label}</div><Typography.Text color="secondary" size="sm">{hint}</Typography.Text></div>
              </SimpleSelectOption>))}
          </SimpleSelect>
        </div>
        {type === "inference_log" && (
          <div>
            <FormUI.Label htmlFor="mon-problem">Problem type</FormUI.Label>
            <Radio.Group componentId="quality.create.problem" name="mon-problem" value={problem} layout="horizontal"
                         onChange={(e) => setProblem(e.target.value)}>
              <Radio value="PROBLEM_TYPE_CLASSIFICATION">Classification</Radio>
              <Radio value="PROBLEM_TYPE_REGRESSION">Regression</Radio>
            </Radio.Group>
          </div>
        )}
        {type === "inference_log" && <>
          {colSelect("prediction_col", "Prediction column")}
          {colSelect("label_col", "Label column", true, "Ground truth. It can stay empty until labels arrive.")}
          {colSelect("model_id_col", "Model ID column")}
          {problem === "PROBLEM_TYPE_CLASSIFICATION" &&
            colSelect("prediction_proba_col", "Prediction probability column", true, "Each class label's probability, for log loss and ROC AUC.")}
        </>}
        {(type === "inference_log" || type === "time_series") && <>
          {colSelect("timestamp_col", "Timestamp column", false, "TIMESTAMP, or a type that converts to one.")}
          <div>
            <FormUI.Label>Metric granularities</FormUI.Label>
            <FormUI.Hint>How the data is split into windows across time.</FormUI.Hint>
            <div css={{ display: "flex", flexWrap: "wrap", gap: `${theme.spacing.xs}px ${theme.spacing.md}px` }}>
              {GRANULARITIES.map((g) => (
                <Checkbox key={g} componentId="quality.create.gran" isChecked={grans.includes(g)}
                          onChange={(on) => setGrans(on ? GRANULARITIES.filter((x) => x === g || grans.includes(x)) : grans.filter((x) => x !== g))}>
                  {g}
                </Checkbox>))}
            </div>
          </div>
        </>}
        {type === "snapshot" && <Typography.Text color="secondary">Each refresh profiles the whole table.</Typography.Text>}
      </>)}
      <div css={{ borderTop: `1px solid ${theme.colors.border}` }} />
      <div css={{ padding: `${theme.spacing.md}px 0` }}>
        <Button componentId="quality.create.advanced" type="link" icon={advanced ? <ChevronDownIcon /> : <ChevronRightIcon />}
                onClick={() => setAdvanced(!advanced)}>
          <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>Advanced options</Typography.Title>
        </Button>
        {!advanced && (
          <Typography.Text color="secondary" css={{ display: "block", marginLeft: 24 }}>
            Schedule, notifications, metrics schema, baseline, slices and custom metrics. Metrics schema: {schema}
          </Typography.Text>
        )}
      </div>
      {advanced && <>
        {section("Schedule", <>
          <Radio.Group componentId="quality.create.schedule" name="mon-schedule" value={scheduled ? "yes" : "no"}
                       onChange={(e) => setScheduled(e.target.value === "yes")}>
            <Radio value="no">Refresh manually</Radio>
            <Radio value="yes">Refresh on schedule</Radio>
          </Radio.Group>
          {scheduled && (
            <div css={{ display: "flex", gap: theme.spacing.sm, flexWrap: "wrap", alignItems: "flex-end" }}>
              <SimpleSelect componentId="quality.create.freq" id="mon-freq" label="Every" value={freq} onChange={(e) => setFreq(e.target.value as Freq)}>
                <SimpleSelectOption value="hour">Hour</SimpleSelectOption>
                <SimpleSelectOption value="day">Day</SimpleSelectOption>
                <SimpleSelectOption value="week">Week</SimpleSelectOption>
                <SimpleSelectOption value="custom">Custom (Quartz cron)</SimpleSelectOption>
              </SimpleSelect>
              {freq === "week" && (
                <SimpleSelect componentId="quality.create.day" id="mon-day" label="On" value={day} onChange={(e) => setDay(e.target.value)}>
                  {DAYS.map((d) => <SimpleSelectOption key={d} value={d}>{d}</SimpleSelectOption>)}
                </SimpleSelect>
              )}
              {(freq === "day" || freq === "week") && <>
                <div><FormUI.Label htmlFor="mon-hour">At</FormUI.Label>
                  <div css={{ display: "flex", gap: 4, alignItems: "center" }}>
                    <Input componentId="quality.create.hour" id="mon-hour" value={hour} onChange={(e) => setHour(e.target.value.replace(/\D/g, "").slice(0, 2))} css={{ width: 56 }} />:
                    <Input componentId="quality.create.minute" id="mon-minute" value={minute.padStart(2, "0")} onChange={(e) => setMinute(e.target.value.replace(/\D/g, "").slice(-2))} css={{ width: 56 }} />
                  </div></div>
              </>}
              {freq === "custom" && (
                <div><FormUI.Label htmlFor="mon-cron">Quartz cron</FormUI.Label>
                  <Input componentId="quality.create.cron" id="mon-cron" value={customCron} placeholder="0 0 6 * * ?" onChange={(e) => setCustomCron(e.target.value)} /></div>
              )}
              <div><FormUI.Label htmlFor="mon-tz">Timezone</FormUI.Label>
                <Input componentId="quality.create.tz" id="mon-tz" value={tz} onChange={(e) => setTz(e.target.value)} css={{ width: 180 }} /></div>
            </div>
          )}
        </>)}
        {section("Notifications", <div>
          <FormUI.Label htmlFor="mon-emails">Email on refresh failure</FormUI.Label>
          <FormUI.Hint>Up to 5 emails are supported per notification event type.</FormUI.Hint>
          <Input componentId="quality.create.emails" id="mon-emails" value={emails} placeholder="user@example.com, …" onChange={(e) => setEmails(e.target.value)} />
        </div>)}
        {section("Metrics", <>
          <div>
            <FormUI.Label htmlFor="mon-schema">Metrics tables schema name</FormUI.Label>
            <FormUI.Hint>project.dataset, in the same BigQuery location as the table. Defaults to the table's dataset.</FormUI.Hint>
            <Input componentId="quality.create.schema" id="mon-schema" value={schema} onChange={(e) => setSchema(e.target.value)} />
          </div>
          <div>
            <FormUI.Label htmlFor="mon-baseline">Baseline table name<Typography.Text color="secondary"> (optional)</Typography.Text></FormUI.Label>
            <FormUI.Hint>A table or view with the same columns as baseline data for comparison, e.g. the validation set scored.</FormUI.Hint>
            <Input componentId="quality.create.baseline" id="mon-baseline" value={baseline} placeholder="project.dataset.table" onChange={(e) => setBaseline(e.target.value)} />
          </div>
          {type !== "snapshot" && (
            <div>
              <FormUI.Label htmlFor="mon-lookback">Lookback days<Typography.Text color="secondary"> (optional)</Typography.Text></FormUI.Label>
              <FormUI.Hint>How far back each refresh recomputes: 30 when empty. Set it past the labels' delay.</FormUI.Hint>
              <Input componentId="quality.create.lookback" id="mon-lookback" value={lookback} placeholder="30" css={{ width: 120 }}
                     onChange={(e) => setLookback(e.target.value.replace(/\D/g, "").slice(0, 3))} />
            </div>
          )}
          <div>
            <FormUI.Label>Metric slicing expressions</FormUI.Label>
            <FormUI.Hint>"col_2 &gt; 10" slices by the predicate and its complement; "col_1" makes one slice per distinct value.</FormUI.Hint>
            {slices.map((s, i) => (
              <div key={i} css={{ display: "flex", gap: theme.spacing.xs, marginBottom: theme.spacing.xs }}>
                <Input componentId="quality.create.slice" value={s} placeholder="country" onChange={(e) => setSlices(slices.map((x, j) => (j === i ? e.target.value : x)))} />
                <Button componentId="quality.create.slice.remove" icon={<CloseSmallIcon />} aria-label="Remove expression" onClick={() => setSlices(slices.filter((_, j) => j !== i))} />
              </div>))}
            <Button componentId="quality.create.slice.add" icon={<PlusIcon />} onClick={() => setSlices([...slices, ""])}>Add expression</Button>
          </div>
          <div>
            <FormUI.Label>Custom metrics</FormUI.Label>
            {metrics.map((c, i) => {
              const set = (patch: Partial<Metric>) => setMetrics(metrics.map((x, j) => (j === i ? { ...x, ...patch } : x)));
              return (
                <div key={i} css={{ border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd, padding: theme.spacing.sm,
                                    marginBottom: theme.spacing.sm, display: "grid", gridTemplateColumns: "1fr 1fr", gap: theme.spacing.sm }}>
                  <div><FormUI.Label>Name</FormUI.Label><Input componentId="quality.create.metric.name" value={c.name} onChange={(e) => set({ name: e.target.value })} /></div>
                  <SimpleSelect componentId="quality.create.metric.type" id={`mon-metric-type-${i}`} label="Type" value={c.type} onChange={(e) => set({ type: e.target.value })}>
                    {METRIC_TYPES.map(([v, l]) => <SimpleSelectOption key={v} value={v}>{l}</SimpleSelectOption>)}
                  </SimpleSelect>
                  <div><FormUI.Label>Input columns</FormUI.Label><Input componentId="quality.create.metric.cols" value={c.input_columns} placeholder=":table, col_a" onChange={(e) => set({ input_columns: e.target.value })} /></div>
                  <SimpleSelect componentId="quality.create.metric.out" id={`mon-metric-out-${i}`} label="Output type" value={c.output_data_type} onChange={(e) => set({ output_data_type: e.target.value })}>
                    {OUTPUT_TYPES.map((t) => <SimpleSelectOption key={t} value={t}>{t}</SimpleSelectOption>)}
                  </SimpleSelect>
                  <div css={{ gridColumn: "1 / -1" }}><FormUI.Label>Definition</FormUI.Label>
                    <Input.TextArea componentId="quality.create.metric.def" value={c.definition} rows={2} placeholder="avg({{prediction_col}} - {{label_col}})"
                                    onChange={(e) => set({ definition: e.target.value })} /></div>
                  <div css={{ gridColumn: "1 / -1" }}>
                    <Button componentId="quality.create.metric.remove" size="small" onClick={() => setMetrics(metrics.filter((_, j) => j !== i))}>Remove</Button>
                  </div>
                </div>
              );
            })}
            <Button componentId="quality.create.metric.add" icon={<PlusIcon />}
                    onClick={() => setMetrics([...metrics, { type: METRIC_TYPES[0][0], name: "", input_columns: ":table", definition: "", output_data_type: "double" }])}>
              Add custom metric
            </Button>
          </div>
        </>)}
      </>}
      {missing && type && <Typography.Text color="secondary" size="sm"><InfoIcon /> {missing}</Typography.Text>}
      {busy && deploy && <Typography.Text color="secondary" size="sm"><InfoIcon /> Deploying {key} to {target}: this takes a few minutes.</Typography.Text>}
      {yaml && (
        <>
          <Spacer size="md" />
          <div css={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <Typography.Text bold>{deploy ? "As a bundle declares it" : "Add this to a bundle and deploy it"}</Typography.Text>
            <Button componentId="quality.create.copy" size="small" icon={<CopyIcon />} onClick={() => navigator.clipboard.writeText(yaml)}>Copy</Button>
          </div>
          <pre css={{ background: theme.colors.backgroundSecondary, padding: theme.spacing.sm, borderRadius: theme.borders.borderRadiusMd,
                      fontSize: 12, overflow: "auto", maxHeight: 320 }}>{yaml}</pre>
        </>
      )}
    </Modal>
  );
}
