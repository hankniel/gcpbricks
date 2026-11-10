import { Modal, Typography, useDesignSystemTheme } from "@databricks/design-system";
import { ALT, MOD } from "./actions";
import { Keys } from "./CommandPalette";

const COMMAND: [string, string][] = [
  ["Enter", "Edit the selected cell"], ["A", "Insert a cell above"], ["B", "Insert a cell below"], ["D , D", "Delete the selected cells"],
  ["Z", "Undo deleting cells"], ["Y", "Change to code"], ["M", "Change to markdown"], ["T", "Add a title"],
  ["L", "Toggle line numbers"], ["↑ / K", "Select the cell above"], ["↓ / J", "Select the cell below"],
  ["Shift + ↑", "Extend the selection up"], ["Shift + ↓", "Extend the selection down"], [`${MOD} + A`, "Select all cells"],
  [`${MOD} + C`, "Copy cells"], [`${MOD} + X`, "Cut cells"], [`${MOD} + V`, "Paste cells below"],
  ["I , I", "Interrupt execution"], ["0 , 0", "Clear state (restart the kernel)"], ["H", "Show keyboard shortcuts"],
];
const EDIT: [string, string][] = [
  ["Esc", "Leave the editor for command mode"], ["Tab", "Complete, or indent"], ["Shift + Tab", "Show the docstring, or outdent"],
  ["Ctrl + Space", "Show completions"], [`${MOD} + /`, "Toggle a line comment"], [`${MOD} + D`, "Select the next occurrence"],
  [`${ALT} + Click`, "Add a cursor"], [`${MOD} + ]`, "Indent"], [`${MOD} + [`, "Outdent"],
  [`${MOD} + Shift + L`, "Select every occurrence"], [`${MOD} + ${ALT} + ↑`, "Add a cursor above"], [`${MOD} + ${ALT} + ↓`, "Add a cursor below"],
  [`${ALT} + ↑`, "Move the line up"], [`${ALT} + ↓`, "Move the line down"], [`${MOD} + I`, "Expand the selection"],
  ["F12", "Go to definition"], [`${MOD} + Click`, "Go to definition"], ["F2", "Rename the symbol"],
  [`${MOD} + Z`, "Undo"], [`${MOD} + Shift + Z`, "Redo"], ["↑ / ↓", "From a first or last line, move into the next cell"],
];
const BOTH: [string, string][] = [
  [`${MOD} + Enter`, "Run the selected cells"], ["Shift + Enter", "Run the cell and select the next"],
  [`${ALT} + Shift + Enter`, "Run all"], [`${ALT} + Shift + ↑`, "Run all above"], [`${ALT} + Shift + ↓`, "Run all below"],
  [`Ctrl + ${ALT} + ↑`, "Move the cell up"], [`Ctrl + ${ALT} + ↓`, "Move the cell down"],
  [`${MOD} + Shift + F`, "Format the selected cells"], [`${MOD} + F`, "Find"], [`Ctrl + ${ALT} + F`, "Replace"], [`${MOD} + S`, "Save"], [`${MOD} + Shift + P`, "Command palette"],
];

function Group({ title, rows }: { title: string; rows: [string, string][] }) {
  const { theme } = useDesignSystemTheme();
  return (
    <div css={{ breakInside: "avoid", marginBottom: theme.spacing.lg }}>
      <Typography.Title level={4} css={{ "&&": { margin: "0 0 8px" } }}>{title}</Typography.Title>
      {rows.map(([k, what]) => (
        <div key={what} css={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, minHeight: 28,
                               borderBottom: `1px solid ${theme.colors.border}` }}>
          <Typography.Text>{what}</Typography.Text><Keys keys={k} />
        </div>
      ))}
    </div>
  );
}

/** The sheet Databricks opens on H: what each key does in command mode and in the editor. */
export default function Shortcuts({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Modal componentId="nb.shortcuts" title="Keyboard shortcuts" visible={open} onCancel={onClose} footer={null} size="wide">
      <div css={{ columnCount: 2, columnGap: 32 }}>
        <Group title="Command mode (Esc)" rows={COMMAND} />
        <Group title="Edit mode (Enter)" rows={EDIT} />
        <Group title="Both modes" rows={BOTH} />
      </div>
    </Modal>
  );
}
