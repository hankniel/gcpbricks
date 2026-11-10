import { Children, useState } from "react";
import type { PointerEvent, ReactNode } from "react";
import { Button, SidebarCollapseIcon, SidebarExpandIcon, Tooltip, useDesignSystemTheme } from "@databricks/design-system";

/** Which panel it is: its width and open state are remembered under `key`, for every page showing it. */
export type PanelKind = { key: string; name: string; width: number; min: number; max: number; sticky?: boolean };

const DETAILS: PanelKind = { key: "mlops-console.details-panel", name: "details", width: 360, min: 280, max: 720 };

function stored({ key, width, min, max }: PanelKind): { width: number; open: boolean } {
  try {
    const v = JSON.parse(localStorage.getItem(key) ?? "{}");
    return { width: typeof v.width === "number" ? Math.min(max, Math.max(min, v.width)) : width, open: v.open !== false };
  } catch {
    return { width, open: true };
  }
}

/** A page's work beside Databricks' details panel: the panel's left edge drags its width, its corner
 *  button folds it away, and both choices carry to every page. Takes two children: work, then panel.
 *  `sticky` keeps the panel in view as the work scrolls (a table of contents). */
export default function SidePanel({ children, top = 0, kind = DETAILS }: { children: ReactNode; top?: number; kind?: PanelKind }) {
  const { theme } = useDesignSystemTheme();
  const { key, name, width: DEFAULT, min: MIN, max: MAX } = kind;
  const [state, setState] = useState(() => stored(kind));
  const save = (next: { width: number; open: boolean }) => {
    setState(next);
    try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* the choice lasts this visit only */ }
  };
  const [work, panel] = Children.toArray(children);
  const drag = (e: PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const x0 = e.clientX, w0 = state.width;
    let w = w0;
    const move = (m: globalThis.PointerEvent) => {
      w = Math.min(MAX, Math.max(MIN, Math.round(w0 - (m.clientX - x0))));
      setState((s) => ({ ...s, width: w }));
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      document.body.style.removeProperty("cursor");
      document.body.style.removeProperty("user-select");
      save({ width: w, open: true });
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  };
  return (
    <div css={{ display: "grid", gridTemplateColumns: `minmax(0, 1fr) ${state.open ? state.width : 32}px`,
                gap: theme.spacing.lg, alignItems: "start" }}>
      <div css={{ minWidth: 0 }}>{work}</div>
      {state.open ? (
        <div css={{ position: kind.sticky ? "sticky" : "relative", top: kind.sticky ? 0 : undefined, minWidth: 0, paddingTop: top }}>
          <div role="separator" aria-orientation="vertical" aria-label={`Resize the ${name} panel`} title="Drag to resize, double-click to reset"
               onPointerDown={drag} onDoubleClick={() => save({ width: DEFAULT, open: true })}
               css={{ position: "absolute", top: 0, bottom: 0, left: -theme.spacing.lg / 2 - 4, width: 8, cursor: "col-resize",
                      touchAction: "none", zIndex: 1,
                      "&::after": { content: '""', position: "absolute", top: 0, bottom: 0, left: 3, width: 2 },
                      "&:hover::after, &:active::after": { background: theme.colors.actionPrimaryBackgroundDefault } }} />
          <div css={{ position: "absolute", top: top + 4, right: 0, zIndex: 1 }}>
            <Tooltip componentId="panel.collapse.tip" content={`Collapse ${name}`}>
              <Button componentId="panel.collapse" size="small" icon={<SidebarCollapseIcon css={{ transform: "scaleX(-1)" }} />}
                      aria-label={`Collapse ${name}`} onClick={() => save({ ...state, open: false })} />
            </Tooltip>
          </div>
          {panel}
        </div>
      ) : (
        <div css={{ position: kind.sticky ? "sticky" : undefined, top: kind.sticky ? 0 : undefined, paddingTop: top }}>
          <Tooltip componentId="panel.expand.tip" content={`Show ${name}`}>
            <Button componentId="panel.expand" size="small" icon={<SidebarExpandIcon css={{ transform: "scaleX(-1)" }} />}
                    aria-label={`Show ${name}`} onClick={() => save({ ...state, open: true })} />
          </Tooltip>
        </div>
      )}
    </div>
  );
}
