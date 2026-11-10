/** One notebook command: the menus, the command palette and the shortcuts sheet all read the same list. */
export type Action = {
  id: string;
  group: "File" | "Edit" | "View" | "Run" | "Help";
  label: string;
  keys?: string;
  run: () => void;
  disabled?: boolean;
  checked?: boolean;
  // Set for a menu's section breaks; the palette ignores it.
  section?: string;
};

export const MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);
export const MOD = MAC ? "⌘" : "Ctrl";
export const ALT = MAC ? "Option" : "Alt";
