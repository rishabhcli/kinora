/**
 * The application-menu *template* as pure data, plus the accelerator registry.
 *
 * Keeping the template a pure function of (platform, callbacks) lets us assert
 * its shape in tests — labels present, accelerators unique, mac-only items
 * gated — without building a real `Menu`. The Electron-bound `menu` service
 * feeds this to `Menu.buildFromTemplate`.
 */
import type { MenuAction } from "../shared/ipc-contract.js";

/** A platform-agnostic description of one accelerator we register. */
export interface AcceleratorBinding {
  accelerator: string;
  action: MenuAction | "add-book" | "reload";
  /** True for `globalShortcut` (works when unfocused). */
  global?: boolean;
}

/**
 * The canonical accelerator table. Menu items reference these so the menu and
 * any global shortcuts can't drift out of sync. CmdOrCtrl maps per-platform.
 */
export const ACCELERATORS: readonly AcceleratorBinding[] = Object.freeze([
  { accelerator: "CmdOrCtrl+O", action: "add-book" },
  { accelerator: "CmdOrCtrl+,", action: "open-settings" },
  { accelerator: "CmdOrCtrl+Shift+D", action: "open-diagnostics" },
  { accelerator: "CmdOrCtrl+Shift+L", action: "open-library" },
  { accelerator: "CmdOrCtrl+N", action: "new-window" },
  { accelerator: "CmdOrCtrl+\\", action: "toggle-director-bar" },
  { accelerator: "CmdOrCtrl+Alt+Shift+K", action: "open-diagnostics", global: true },
]);

/** Assert no two bindings share an accelerator (guards against silent clobber). */
export function findDuplicateAccelerators(
  bindings: readonly AcceleratorBinding[] = ACCELERATORS,
): string[] {
  const seen = new Set<string>();
  const dupes = new Set<string>();
  for (const b of bindings) {
    const key = b.accelerator.toLowerCase();
    if (seen.has(key)) dupes.add(b.accelerator);
    seen.add(key);
  }
  return [...dupes];
}

export const GLOBAL_SHORTCUTS = ACCELERATORS.filter((b) => b.global);

/** Callbacks the template wires to menu clicks. */
export interface MenuCallbacks {
  addBook: () => void;
  menuAction: (action: MenuAction) => void;
  openExternal: (url: string) => void;
  checkForUpdates: () => void;
}

/**
 * Build the menu template. Returns Electron's
 * `MenuItemConstructorOptions[]` shape, but typed loosely (`MenuTemplate`) so
 * this module needn't import `electron` — the service casts it.
 */
export type MenuTemplate = Array<Record<string, unknown>>;

export function buildMenuTemplate(platform: NodeJS.Platform, cb: MenuCallbacks): MenuTemplate {
  const isMac = platform === "darwin";
  const accel = (action: AcceleratorBinding["action"]): string | undefined =>
    ACCELERATORS.find((b) => b.action === action && !b.global)?.accelerator;

  const template: MenuTemplate = [];

  if (isMac) {
    template.push({
      label: "Kinora",
      submenu: [
        { role: "about" },
        { type: "separator" },
        {
          label: "Check for Updates…",
          click: () => cb.checkForUpdates(),
        },
        {
          label: "Settings…",
          accelerator: accel("open-settings"),
          click: () => cb.menuAction("open-settings"),
        },
        { type: "separator" },
        { role: "services" },
        { type: "separator" },
        { role: "hide" },
        { role: "hideOthers" },
        { role: "unhide" },
        { type: "separator" },
        { role: "quit" },
      ],
    });
  }

  template.push({
    label: "File",
    submenu: [
      { label: "Add Book…", accelerator: accel("add-book"), click: () => cb.addBook() },
      { label: "New Window", accelerator: accel("new-window"), click: () => cb.menuAction("new-window") },
      { type: "separator" },
      ...(isMac
        ? [{ role: "close" }]
        : [
            { label: "Settings…", accelerator: accel("open-settings"), click: () => cb.menuAction("open-settings") },
            { type: "separator" },
            { role: "quit" },
          ]),
    ],
  });

  template.push({
    label: "Edit",
    submenu: [
      { role: "undo" },
      { role: "redo" },
      { type: "separator" },
      { role: "cut" },
      { role: "copy" },
      { role: "paste" },
      { role: "selectAll" },
    ],
  });

  template.push({
    label: "View",
    submenu: [
      { label: "Library", accelerator: accel("open-library"), click: () => cb.menuAction("open-library") },
      {
        label: "Toggle Director Bar",
        accelerator: accel("toggle-director-bar"),
        click: () => cb.menuAction("toggle-director-bar"),
      },
      { type: "separator" },
      { role: "reload" },
      { role: "forceReload" },
      { role: "toggleDevTools" },
      { type: "separator" },
      { role: "resetZoom" },
      { role: "zoomIn" },
      { role: "zoomOut" },
      { type: "separator" },
      { role: "togglefullscreen" },
    ],
  });

  template.push({ role: "windowMenu" });

  template.push({
    role: "help",
    submenu: [
      {
        label: "Diagnostics…",
        accelerator: accel("open-diagnostics"),
        click: () => cb.menuAction("open-diagnostics"),
      },
      { type: "separator" },
      { label: "Kinora on GitHub", click: () => cb.openExternal("https://github.com/rishabhcli/kinora") },
      ...(!isMac ? [{ label: "Check for Updates…", click: () => cb.checkForUpdates() }] : []),
    ],
  });

  return template;
}

/** Flatten a template to its leaf labels (for assertions). */
export function collectLabels(template: MenuTemplate): string[] {
  const out: string[] = [];
  const walk = (items: unknown) => {
    if (!Array.isArray(items)) return;
    for (const item of items) {
      if (item && typeof item === "object") {
        const rec = item as Record<string, unknown>;
        if (typeof rec.label === "string") out.push(rec.label);
        if (rec.submenu) walk(rec.submenu);
      }
    }
  };
  walk(template);
  return out;
}
