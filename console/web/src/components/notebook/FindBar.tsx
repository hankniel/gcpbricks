import { useEffect, useRef, type ComponentRef } from "react";
import {
  ArrowDownIcon, ArrowUpIcon, Button, ChevronDownIcon, ChevronRightIcon, CloseIcon, Input, Tooltip, Typography, useDesignSystemTheme,
} from "@databricks/design-system";

export type FindState = { q: string; replace: string; withReplace: boolean; caseSensitive: boolean; regex: boolean; n: number };
export const NO_FIND: FindState = { q: "", replace: "", withReplace: false, caseSensitive: false, regex: false, n: 0 };

/** The query as a global RegExp, or null while it's empty or not yet a valid pattern. */
export function findRegExp(f: FindState): RegExp | null {
  if (!f.q) return null;
  try {
    return new RegExp(f.regex ? f.q : f.q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), f.caseSensitive ? "g" : "gi");
  } catch {
    return null;
  }
}

/** Databricks' notebook-wide find and replace, docked at the top right of the notebook. */
export default function FindBar({ state, set, total, at, onStep, onReplace, onReplaceAll, onClose, canEdit }: {
  state: FindState; set: (f: FindState) => void; total: number; at: number;
  onStep: (dir: 1 | -1) => void; onReplace: () => void; onReplaceAll: () => void; onClose: () => void; canEdit: boolean;
}) {
  const { theme } = useDesignSystemTheme();
  const box = useRef<ComponentRef<typeof Input>>(null);
  useEffect(() => { box.current?.focus(); box.current?.select(); }, [state.n]);
  const toggle = (label: string, title: string, on: boolean, flip: () => void) => (
    <Tooltip componentId="nb.find.toggle.tip" content={title}>
      <button type="button" aria-pressed={on} aria-label={title} onClick={flip}
              css={{ all: "unset", cursor: "pointer", fontFamily: "monospace", fontSize: 12, padding: "0 5px", lineHeight: "20px", borderRadius: 4,
                     color: on ? theme.colors.actionDefaultTextHover : theme.colors.textSecondary,
                     background: on ? theme.colors.actionDefaultBackgroundPress : undefined, border: `1px solid ${on ? theme.colors.actionDefaultBorderFocus : "transparent"}` }}>
        {label}</button>
    </Tooltip>
  );
  const keys = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); onClose(); }
    else if (e.key === "Enter") { e.preventDefault(); onStep(e.shiftKey ? -1 : 1); }
  };
  return (
    <div role="search" css={{ position: "sticky", top: 52, zIndex: 4, marginLeft: "auto", width: "fit-content", display: "flex", gap: 4,
                              padding: 6, background: theme.colors.backgroundPrimary, border: `1px solid ${theme.colors.border}`,
                              borderRadius: 8, boxShadow: theme.shadows.md }}>
      <Button componentId="nb.find.expand" size="small" icon={state.withReplace ? <ChevronDownIcon /> : <ChevronRightIcon />}
              aria-label="Toggle replace" onClick={() => set({ ...state, withReplace: !state.withReplace })} disabled={!canEdit} />
      <div css={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <div css={{ display: "flex", alignItems: "center", gap: 4 }}>
          <div css={{ width: 260 }}>
            <Input ref={box} componentId="nb.find.q" size="small" placeholder="Find" value={state.q} onKeyDown={keys}
                   onChange={(e) => set({ ...state, q: e.target.value })}
                   suffix={<span css={{ display: "inline-flex", gap: 2 }}>
                     {toggle("Aa", "Match case", state.caseSensitive, () => set({ ...state, caseSensitive: !state.caseSensitive }))}
                     {toggle(".*", "Use regular expression", state.regex, () => set({ ...state, regex: !state.regex }))}
                   </span>} />
          </div>
          <Typography.Text color="secondary" size="sm" css={{ minWidth: 72, textAlign: "center" }}>
            {state.q ? (total ? `${at + 1} of ${total}` : "No results") : ""}</Typography.Text>
          <Button componentId="nb.find.prev" size="small" icon={<ArrowUpIcon />} aria-label="Previous match" disabled={!total} onClick={() => onStep(-1)} />
          <Button componentId="nb.find.next" size="small" icon={<ArrowDownIcon />} aria-label="Next match" disabled={!total} onClick={() => onStep(1)} />
          <Button componentId="nb.find.close" size="small" icon={<CloseIcon />} aria-label="Close find" onClick={onClose} />
        </div>
        {state.withReplace && (
          <div css={{ display: "flex", alignItems: "center", gap: 4 }}>
            <div css={{ width: 260 }}>
              <Input componentId="nb.find.replace" size="small" placeholder="Replace" value={state.replace}
                     onChange={(e) => set({ ...state, replace: e.target.value })}
                     onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); onReplace(); } else if (e.key === "Escape") onClose(); }} />
            </div>
            <Button componentId="nb.find.replace.one" size="small" disabled={!total} onClick={onReplace}>Replace</Button>
            <Button componentId="nb.find.replace.all" size="small" disabled={!total} onClick={onReplaceAll}>Replace all</Button>
          </div>
        )}
      </div>
    </div>
  );
}
