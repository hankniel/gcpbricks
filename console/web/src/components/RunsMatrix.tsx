import { Link } from "react-router-dom";
import { Tooltip, Typography, useDesignSystemTheme } from "@databricks/design-system";
import { RunSummary } from "../api";
import { useStateFill } from "./Status";
import { TIME_ZONE, duration, label, runState, tickText, when } from "../util";

const secs = (r: RunSummary) => Math.max(1, ((r.end ? Date.parse(r.end) : Date.now()) - Date.parse(r.start)) / 1000);

/** "1h 13m 1s": the matrix's axis names the longest run to the second, as Databricks' does. */
const full = (s: number) => [[Math.floor(s / 3600), "h"], [Math.floor((s % 3600) / 60), "m"], [Math.round(s % 60), "s"]]
  .filter(([n], i, all) => n || (i === all.length - 1 && s < 60)).map(([n, u]) => `${n}${u}`).join(" ");

const day = (iso: string) => new Date(iso).toLocaleDateString("en-US", { timeZone: TIME_ZONE, month: "short", day: "2-digit" });

/** Databricks' run matrix: each run's duration as a bar, a date on the axis every few runs, and each
 *  task's state below it; every bar and square opens that run or task. */
export default function RunsMatrix({ wf, runs, tasks }: { wf: string; runs: RunSummary[]; tasks: string[] }) {
  const { theme } = useDesignSystemTheme();
  const base = useStateFill();
  // The chart's own colors in Databricks, failures hatched; other states keep the console's.
  const hatch = "repeating-linear-gradient(-45deg, rgba(255,255,255,0.25), rgba(255,255,255,0.25) 1px, transparent 1px, transparent 6px)";
  const paint = (s?: string | null) => {
    switch (s) {
      case "SUCCEEDED": case "SUCCESS": case "REUSED": return { background: "#308813" };
      case "SUCCEEDED_WITH_FAILURES": return { background: "#C8A12B" };
      case "FAILED": return { background: "#CC3352", backgroundImage: hatch };
      case "UPSTREAM_FAILED": return { background: "#F69CAD" };
      default: return { background: base(s) };
    }
  };
  const cols = runs.slice(0, 30).reverse();
  const top = Math.max(...cols.map(secs));
  const COL = 24, AXIS = 20, LEFT = 200, CHART = 104, HEAD = 32, ROW = 24;
  const grid = { display: "grid", gridTemplateColumns: `${LEFT - AXIS}px repeat(${cols.length}, ${COL}px)` } as const;
  const line = `1px solid ${theme.colors.border}`;
  // A date over a run when it starts a new day at least four runs past the last one labelled.
  const marks = cols.reduce<number[]>((m, r, i) =>
    !m.length || (i - m[m.length - 1] >= 4 && day(r.start) !== day(cols[m[m.length - 1]].start)) ? [...m, i] : m, []);
  const vertical = (text: string, height: number) => (
    <div css={{ width: AXIS, height, display: "flex", alignItems: "center", justifyContent: "center", overflow: "hidden" }}>
      <Typography.Text color="secondary" css={{ transform: "rotate(-90deg)", whiteSpace: "nowrap" }}>{text}</Typography.Text>
    </div>
  );
  return (
    <div css={{ overflowX: "auto", padding: `0 ${theme.spacing.sm}px ${theme.spacing.sm}px 0` }}>
      <div css={{ minWidth: LEFT + cols.length * COL, display: "grid", gridTemplateColumns: `${AXIS}px 1fr` }}>
        <div css={{ gridRow: "2" }}>{vertical("Run total duration", CHART + theme.spacing.md)}</div>
        <div css={{ gridColumn: "2", ...grid, height: HEAD, alignItems: "center" }}>
          <span />
          {cols.map((r, i) => (
            <span key={r.id} css={{ position: "relative", height: "100%" }}>
              {marks.includes(i) && <Typography.Text css={{ position: "absolute", top: "50%", left: "50%",
                transform: "translate(-50%, -50%)", whiteSpace: "nowrap" }}>{day(r.start)}</Typography.Text>}
            </span>
          ))}
        </div>
        <div css={{ gridColumn: "2", ...grid, height: CHART + theme.spacing.md, position: "relative", borderBottom: line }}>
          <div css={{ position: "relative", borderRight: line }}>
            {[top / 2, top].map((t) => (
              <Typography.Text key={t} css={{ position: "absolute", right: theme.spacing.sm, bottom: (t / top) * CHART,
                                              transform: "translateY(50%)", whiteSpace: "nowrap" }}>{full(t)}</Typography.Text>
            ))}
          </div>
          {[top / 2, top].map((t) => (
            <span key={t} css={{ position: "absolute", left: LEFT - AXIS, right: 0, bottom: (t / top) * CHART,
                                 borderTop: line, pointerEvents: "none" }} />
          ))}
          {cols.map((r, i) => {
            const s = runState(r);
            return (
              <div key={r.id} css={{ display: "flex", alignItems: "flex-end", justifyContent: "center", position: "relative",
                                     borderLeft: marks.includes(i) ? line : undefined, marginLeft: marks.includes(i) ? -1 : 0 }}>
                <Tooltip componentId="matrix.bar" content={`${label[s] ?? s} · ${when(r.start, true)} · ${duration(r.start, r.end)}`}>
                  <Link to={`/jobs/${wf}/runs/${r.id}`} aria-label={`${label[s] ?? s} run of ${when(r.start, true)}`}
                        css={{ display: "block", width: 16, height: Math.max(4, (secs(r) / top) * CHART), borderRadius: 4,
                               ...paint(s), opacity: s === "ACTIVE" ? 0.6 : 1 }} />
                </Tooltip>
              </div>
            );
          })}
        </div>
        <div css={{ gridRow: `3 / span ${Math.max(1, tasks.length)}` }}>{vertical("Tasks", tasks.length * ROW)}</div>
        {tasks.map((t) => (
          <div key={t} css={{ gridColumn: "2", ...grid, height: ROW, alignItems: "center", borderBottom: line }}>
            <Typography.Text ellipsis title={t} css={{ paddingRight: theme.spacing.sm, borderRight: line, height: "100%",
                                                       display: "flex", alignItems: "center" }}>{t}</Typography.Text>
            {cols.map((r) => {
              const st = r.task_states?.[t];
              const secs = r.task_seconds?.[t];
              const took = secs == null ? "" : ` · ${tickText(secs < 3600 ? Math.round(secs) : Math.round(secs / 60) * 60)}`;
              const text = `${t}: ${st ? label[st] ?? st : "Not run"}${took} · ${when(r.start, true)}`;
              const cell = { display: "block", justifySelf: "center", width: 16, height: 16, borderRadius: 4,
                             ...paint(st), opacity: st === "EXCLUDED" || !st ? 0.45 : 1 } as const;
              return (
                <Tooltip key={r.id} componentId="matrix.task" content={text}>
                  {st ? <Link to={`/jobs/${wf}/runs/${r.id}/tasks/${encodeURIComponent(t)}`} aria-label={text} css={cell} />
                      : <span aria-label={text} css={cell} />}
                </Tooltip>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
