/**
 * Application menu, global shortcuts, and the tray icon.
 *
 * The menu *shape* comes from the pure `menu-template`; this service binds it
 * to a real `Menu`, registers the `global` accelerators via `globalShortcut`,
 * and builds a tray with quick actions. All user-triggered actions funnel
 * through the same {@link MenuCallbacks} the renderer also receives over IPC,
 * so a menu item and a renderer button do the identical thing.
 */
import { Menu, Tray, globalShortcut, type MenuItemConstructorOptions } from "electron";
import {
  GLOBAL_SHORTCUTS,
  buildMenuTemplate,
  type MenuCallbacks,
} from "../core/menu-template.js";
import type { MenuAction } from "../shared/ipc-contract.js";
import type { ScopedLogger } from "../core/logger.js";

export interface MenuServiceDeps extends MenuCallbacks {
  log: ScopedLogger;
  icon: Electron.NativeImage;
}

export class MenuService {
  private readonly deps: MenuServiceDeps;
  private tray: Tray | null = null;

  constructor(deps: MenuServiceDeps) {
    this.deps = deps;
  }

  installMenu(): void {
    const template = buildMenuTemplate(process.platform, this.deps) as unknown as MenuItemConstructorOptions[];
    Menu.setApplicationMenu(Menu.buildFromTemplate(template));
  }

  registerGlobalShortcuts(): void {
    for (const binding of GLOBAL_SHORTCUTS) {
      try {
        const ok = globalShortcut.register(binding.accelerator, () => {
          this.deps.menuAction(binding.action as MenuAction);
        });
        if (!ok) this.deps.log.warn("global shortcut not registered", { accelerator: binding.accelerator });
      } catch (err) {
        this.deps.log.warn("global shortcut error", { accelerator: binding.accelerator, message: String(err) });
      }
    }
  }

  unregisterGlobalShortcuts(): void {
    try {
      globalShortcut.unregisterAll();
    } catch {
      /* ignore */
    }
  }

  installTray(): void {
    try {
      const image = this.deps.icon.isEmpty() ? this.deps.icon : this.deps.icon.resize({ width: 18, height: 18 });
      this.tray = new Tray(image);
      this.tray.setToolTip("Kinora — Watch the book");
      const menu = Menu.buildFromTemplate([
        { label: "Open Library", click: () => this.deps.menuAction("open-library") },
        { label: "Add Book…", click: () => this.deps.addBook() },
        { type: "separator" },
        { label: "Diagnostics…", click: () => this.deps.menuAction("open-diagnostics") },
        { label: "Check for Updates…", click: () => this.deps.checkForUpdates() },
        { type: "separator" },
        { role: "quit" },
      ]);
      this.tray.setContextMenu(menu);
      this.tray.on("click", () => this.deps.menuAction("open-library"));
    } catch (err) {
      this.deps.log.warn("tray unavailable", { message: String(err) });
    }
  }

  dispose(): void {
    this.unregisterGlobalShortcuts();
    if (this.tray) {
      this.tray.destroy();
      this.tray = null;
    }
  }
}
