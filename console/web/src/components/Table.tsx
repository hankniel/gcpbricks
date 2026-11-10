import { createContext, useContext, useState } from "react";
import type { ComponentProps, PointerEvent } from "react";
import { Table as DsTable, TableHeader as DsTableHeader } from "@databricks/design-system";

const Resize = createContext<((i: number, px: number | null) => void) | null>(null);

/** The design system's Table whose columns take a width dragged on their header's edge: the
 *  rows are flexboxes, so a width set on a column's n-th cell holds in every row. */
export function Table(props: ComponentProps<typeof DsTable>) {
  const [widths, setWidths] = useState<Record<number, number>>({});
  const set = (i: number, px: number | null) => setWidths((w) => {
    const next = { ...w };
    if (px == null) delete next[i];
    else next[i] = px;
    return next;
  });
  const rules = Object.fromEntries(Object.entries(widths).map(([i, px]) => [
    `& [role=row] > :nth-child(${Number(i) + 1})`,
    { flex: `0 0 ${px}px !important`, width: `${px}px !important`, maxWidth: "none !important", minWidth: "0 !important" }]));
  return (
    <Resize.Provider value={set}>
      <div css={{ "& [role=columnheader]": { position: "relative" }, ...rules }}><DsTable {...props} /></div>
    </Resize.Provider>
  );
}

/** A header whose right edge drags its column's width; a double click gives the width back. */
export function TableHeader({ children, ...props }: ComponentProps<typeof DsTableHeader>) {
  const set = useContext(Resize);
  const start = (e: PointerEvent<HTMLSpanElement>) => {
    e.preventDefault();
    e.stopPropagation();
    const cell = e.currentTarget.closest("[role=columnheader]") as HTMLElement | null;
    if (!cell || !set) return;
    const i = Array.from(cell.parentElement!.children).indexOf(cell);
    const x0 = e.clientX, w0 = cell.getBoundingClientRect().width;
    const move = (m: globalThis.PointerEvent) => set(i, Math.max(48, Math.round(w0 + m.clientX - x0)));
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      document.body.style.removeProperty("cursor");
      document.body.style.removeProperty("user-select");
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  };
  return (
    <DsTableHeader {...props}>
      {children}
      {set && (
        <span role="separator" aria-orientation="vertical" title="Drag to resize, double-click to reset"
              onPointerDown={start} onClick={(e) => e.stopPropagation()}
              onDoubleClick={(e) => {
                e.stopPropagation();
                const cell = e.currentTarget.closest("[role=columnheader]") as HTMLElement | null;
                if (cell) set(Array.from(cell.parentElement!.children).indexOf(cell), null);
              }}
              css={{ position: "absolute", top: 0, right: 0, bottom: 0, width: 8, cursor: "col-resize", zIndex: 1,
                     touchAction: "none", "&:hover, &:active": { boxShadow: "inset -2px 0 0 #2272B4" } }} />
      )}
    </DsTableHeader>
  );
}
