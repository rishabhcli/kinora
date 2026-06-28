import Foundation

/// The single source of truth for the JS <-> native bridge surface.
///
/// Mirrors the Electron preload contract (`apps/desktop/electron/preload.ts`): the
/// renderer (`apps/desktop/src/main.tsx`) reads `window.__KINORA_NATIVE__` to go
/// translucent so the genuine `NSGlassEffectView` shows through. We set that *same*
/// flag, and additionally expose the richer `window.kinora` object the CLAUDE.md
/// native-shell note promises ("token bridge + openBook").
///
/// The contract is **non-invasive**: the bridge never force-writes the page's auth
/// state. It observes `setToken`/`clearToken` (so the OS Keychain mirrors login) and
/// answers `getToken` from the Keychain *or* the page's own `localStorage["kinora.token"]`
/// — whichever the renderer prefers (`api.ts` reads localStorage first).
public enum BridgeContract {
    /// `WKScriptMessageHandler` names the JS shim posts to. Kept as raw strings in one
    /// place so the Swift handler registration and the injected JS never drift.
    public enum MessageName: String, CaseIterable, Sendable {
        case setToken          // { token: string }      fire-and-forget
        case clearToken        //                        fire-and-forget
        case getToken          // ()  -> string|null     reply handler
        case notify            // { title, body, id? }   fire-and-forget
        case setBadge          // { count: number }       fire-and-forget
        case openExternal      // { url: string }         fire-and-forget
        case ready             //                        fire-and-forget (flush queue)
        case log               // { level, message }      fire-and-forget (renderer→native console)
        case importFile        // ()  -> opens native open-panel; reply: path|null
    }

    /// The localStorage key the renderer keeps its bearer token under (`api.ts`).
    /// The bridge reads/writes it so native and web token state stay consistent.
    public static let webTokenKey = "kinora.token"

    /// The native→JS hook the shell calls to navigate the renderer to a book. The JS
    /// shim queues calls until the renderer registers `kinora.onOpenBook(...)` and signals
    /// `kinora.ready()`.
    public static let openBookJSFunction = "window.__kinoraDispatchOpenBook"

    /// The native→JS hook for an import intent (file-drop / Open-With).
    public static let importJSFunction = "window.__kinoraDispatchImport"

    /// The native→JS hook for an arbitrary route navigation.
    public static let routeJSFunction = "window.__kinoraDispatchRoute"

    /// Semantic version reported to the page as `window.kinora.version`.
    public static let shellVersion = "1.0.0"

    /// The platform string reported as `window.kinora.platform`.
    public static let platform = "macos-native"

    /// The JS user-script injected at `documentStart` (before the React bundle). It
    /// defines `window.__KINORA_NATIVE__` + `window.kinora`, marshals to the message
    /// handlers, and queues deep links until the renderer is ready.
    ///
    /// `getToken`/`importFile` use the reply-style handler (returns a Promise); the
    /// rest are fire-and-forget. A small handler-presence guard keeps the shim from
    /// throwing if it is ever evaluated outside the native host (defensive).
    public static func userScriptSource(version: String = shellVersion) -> String {
        // NOTE: kept dependency-free + idempotent (guards re-injection on bf-cache restore).
        """
        (function () {
          if (window.__kinoraBridgeInstalled) { return; }
          window.__kinoraBridgeInstalled = true;

          // Same flag the Electron preload sets — drives html.kinora-native translucency.
          try { Object.defineProperty(window, "__KINORA_NATIVE__", { value: true, configurable: false }); }
          catch (e) { window.__KINORA_NATIVE__ = true; }

          function hasHandler(name) {
            return !!(window.webkit
              && window.webkit.messageHandlers
              && window.webkit.messageHandlers[name]);
          }
          function post(name, payload) {
            if (hasHandler(name)) {
              try { window.webkit.messageHandlers[name].postMessage(payload || {}); }
              catch (e) { /* host gone */ }
            }
          }
          function postReply(name, payload) {
            if (hasHandler(name)) {
              try { return window.webkit.messageHandlers[name].postMessage(payload || {}); }
              catch (e) { return Promise.resolve(null); }
            }
            return Promise.resolve(null);
          }

          // ---- deep-link queue: native may fire openBook before React mounts ----
          var openBookHandlers = [];
          var importHandlers = [];
          var routeHandlers = [];
          var pendingOpenBook = [];
          var pendingImport = [];
          var pendingRoute = [];
          var rendererReady = false;

          window.__kinoraDispatchOpenBook = function (id) {
            if (!rendererReady || openBookHandlers.length === 0) { pendingOpenBook.push(id); return; }
            openBookHandlers.forEach(function (h) { try { h(id); } catch (e) {} });
          };
          window.__kinoraDispatchImport = function (descriptor) {
            if (!rendererReady || importHandlers.length === 0) { pendingImport.push(descriptor); return; }
            importHandlers.forEach(function (h) { try { h(descriptor); } catch (e) {} });
          };
          window.__kinoraDispatchRoute = function (path) {
            if (!rendererReady || routeHandlers.length === 0) { pendingRoute.push(path); return; }
            routeHandlers.forEach(function (h) { try { h(path); } catch (e) {} });
          };

          function flush() {
            if (!rendererReady) { return; }
            var ob = pendingOpenBook; pendingOpenBook = [];
            ob.forEach(function (id) { window.__kinoraDispatchOpenBook(id); });
            var im = pendingImport; pendingImport = [];
            im.forEach(function (d) { window.__kinoraDispatchImport(d); });
            var rt = pendingRoute; pendingRoute = [];
            rt.forEach(function (p) { window.__kinoraDispatchRoute(p); });
          }

          window.kinora = {
            native: true,
            platform: "\(platform)",
            version: "\(version)",

            getToken: function () {
              // Prefer the page's own token (api.ts source of truth), else ask native.
              try {
                var local = window.localStorage.getItem("\(webTokenKey)");
                if (local) { return Promise.resolve(local); }
              } catch (e) {}
              return Promise.resolve(postReply("\(MessageName.getToken.rawValue)", {})).then(function (t) {
                return t || null;
              });
            },
            setToken: function (t) {
              try { if (t) { window.localStorage.setItem("\(webTokenKey)", t); } } catch (e) {}
              post("\(MessageName.setToken.rawValue)", { token: t || "" });
            },
            clearToken: function () {
              try { window.localStorage.removeItem("\(webTokenKey)"); } catch (e) {}
              post("\(MessageName.clearToken.rawValue)", {});
            },

            openBook: function (id) { window.__kinoraDispatchOpenBook(String(id)); },
            onOpenBook: function (cb) { if (typeof cb === "function") { openBookHandlers.push(cb); flush(); } },
            onImport: function (cb) { if (typeof cb === "function") { importHandlers.push(cb); flush(); } },
            onRoute: function (cb) { if (typeof cb === "function") { routeHandlers.push(cb); flush(); } },

            notify: function (opts) {
              opts = opts || {};
              post("\(MessageName.notify.rawValue)", {
                title: opts.title || "Kinora",
                body: opts.body || "",
                id: opts.id || null
              });
            },
            setBadge: function (n) { post("\(MessageName.setBadge.rawValue)", { count: Number(n) || 0 }); },
            openExternal: function (url) { post("\(MessageName.openExternal.rawValue)", { url: String(url) }); },
            importFile: function () {
              return Promise.resolve(postReply("\(MessageName.importFile.rawValue)", {})).then(function (p) {
                return p || null;
              });
            },
            ready: function () {
              rendererReady = true;
              post("\(MessageName.ready.rawValue)", {});
              flush();
            }
          };

          // Bridge console → native unified logging (helps debug a headless web shell).
          ["log", "warn", "error"].forEach(function (level) {
            var orig = console[level] ? console[level].bind(console) : function () {};
            console[level] = function () {
              try {
                var msg = Array.prototype.slice.call(arguments).map(String).join(" ");
                post("\(MessageName.log.rawValue)", { level: level, message: msg });
              } catch (e) {}
              return orig.apply(null, arguments);
            };
          });

          // If the renderer never calls ready() (older build), auto-arm shortly after load
          // so deep links still flush. ready() remains the precise signal.
          window.addEventListener("load", function () {
            setTimeout(function () { if (!rendererReady) { rendererReady = true; flush(); } }, 1200);
          });
        })();
        """
    }
}
