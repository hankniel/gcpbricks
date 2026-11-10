import { useEffect, useMemo, useRef, useState } from "react";
import { CheckIcon, Input, SearchIcon, Typography, useDesignSystemTheme } from "@databricks/design-system";
import { MOD, type Action } from "./actions";

/** A shortcut's keys as Databricks prints them: one chip per key. */
export function Keys({ keys }: { keys: string }) {
  const { theme } = useDesignSystemTheme();
  return (
    <span css={{ display: "inline-flex", gap: 2, flex: "none" }}>
      {keys.split(" ").map((k, i) => k === "," || k === "+" ? <span key={i} css={{ color: theme.colors.textSecondary, padding: "0 1px" }}>{k}</span> : (
        <kbd key={i} css={{ fontFamily: "inherit", fontSize: 11, lineHeight: "18px", minWidth: 18, padding: "0 5px", textAlign: "center",
                           borderRadius: 4, background: theme.colors.backgroundSecondary, border: `1px solid ${theme.colors.border}`,
                           color: theme.colors.textSecondary }}>{k}</kbd>
      ))}
    </span>
  );
}

/** Databricks' command palette (Mod+Shift+P): every notebook action, filtered as you type. */
export default function CommandPalette({ open, onClose, actions }: { open: boolean; onClose: () => void; actions: Action[] }) {
  const { theme } = useDesignSystemTheme();
  const [q, setQ] = useState("");
  const [at, setAt] = useState(0);
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => { if (open) { setQ(""); setAt(0); } }, [open]);
  const shown = useMemo(() => {
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    return actions.filter((a) => !a.disabled && words.every((w) => `${a.group} ${a.label}`.toLowerCase().includes(w)))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [actions, q]);
  useEffect(() => { list.current?.querySelector(`[data-i="${at}"]`)?.scrollIntoView({ block: "nearest" }); }, [at]);
  if (!open) return null;
  const run = (a?: Action) => { if (!a) return; onClose(); setTimeout(a.run, 0); };
  return (
    <div onMouseDown={onClose} css={{ position: "fixed", inset: 0, zIndex: 1000 }}>
      <div onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-label="Command palette"
           css={{ position: "absolute", top: 110, left: "50%", transform: "translateX(-50%)", width: "min(780px, calc(100vw - 32px))",
                  background: theme.colors.backgroundPrimary, border: `1px solid ${theme.colors.border}`, borderRadius: 8,
                  boxShadow: theme.shadows.lg, overflow: "hidden" }}>
        <div css={{ padding: 8, borderBottom: `1px solid ${theme.colors.border}` }}>
          <Input componentId="nb.palette.q" autoFocus prefix={<SearchIcon />} placeholder={`Search actions (${MOD} + Shift + P)...`} value={q}
                 onChange={(e) => { setQ(e.target.value); setAt(0); }}
                 onKeyDown={(e) => {
                   if (e.key === "ArrowDown") { e.preventDefault(); setAt((i) => Math.min(shown.length - 1, i + 1)); }
                   else if (e.key === "ArrowUp") { e.preventDefault(); setAt((i) => Math.max(0, i - 1)); }
                   else if (e.key === "Enter") { e.preventDefault(); run(shown[at]); }
                   else if (e.key === "Escape") { e.preventDefault(); onClose(); }
                 }} />
        </div>
        <div ref={list} role="listbox" css={{ maxHeight: 420, overflow: "auto", padding: "4px 0" }}>
          {shown.length === 0 && <div css={{ padding: "8px 16px" }}><Typography.Text color="secondary">No matching actions</Typography.Text></div>}
          {shown.map((a, i) => (
            <div key={a.id} data-i={i} role="option" aria-selected={i === at} onMouseEnter={() => setAt(i)} onClick={() => run(a)}
                 css={{ display: "flex", alignItems: "center", gap: 8, height: 32, padding: "0 16px 0 12px", cursor: "pointer",
                        background: i === at ? theme.colors.actionDefaultBackgroundHover : undefined }}>
              <span css={{ width: 16, display: "flex", color: theme.colors.textSecondary }}>{a.checked && <CheckIcon />}</span>
              <span css={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {a.group === "File" || a.group === "Help" ? a.label : `${a.group} → ${a.label}`}</span>
              {a.keys && <Keys keys={a.keys} />}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
