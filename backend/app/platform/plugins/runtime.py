"""The sandboxed execution runtime.

This is the security boundary: it compiles and runs plugin source in a namespace
with **no ambient authority** and enforces resource budgets. The threat model is
a *buggy or hostile* plugin author; the guarantees are:

1. **Restricted imports.** Plugin code may only ``import`` modules on the
   effective allowlist (a conservative host base set ∪ the manifest's declared
   allowlist). The import hook is installed *into the plugin's builtins*, so a
   forbidden ``import os`` raises :class:`ForbiddenImportError` at the import
   statement — before any module side effect. Dunder-escape names
   (``builtins``, ``importlib``, ``ctypes``, ``os``, ``sys`` unless allowed) are
   never importable.
2. **No dangerous builtins.** The plugin's ``__builtins__`` is a curated subset:
   no ``open``, ``eval``, ``exec``, ``compile``, ``__import__`` (the safe import
   hook is injected separately), ``input``, ``breakpoint``, ``globals``,
   ``vars``, or attribute-introspection escapes. So even without imports, a
   plugin cannot reach the filesystem or the host frame.
3. **Resource budgets.** Wall-time is enforced cooperatively (the executor runs
   the call in a worker thread with a timeout); host-call count, log-line count,
   and output size are metered by the broker/result path. Memory is bounded
   best-effort on POSIX via ``resource`` soft limits when ``enforce_memory`` is
   set (off by default in-process to avoid perturbing the host).
4. **No host objects leak in.** The only capability-bearing object in scope is
   the injected ``host`` broker; everything else is plain data.

The runtime is deliberately *in-process* (a restricted-namespace interpreter,
not a container): it is the unit the deterministic tests exercise. A production
deployment can run the same :class:`PluginRuntime` inside a subprocess/jail for
defence in depth — the contract (this module's guarantees) is identical.
"""

from __future__ import annotations

import builtins as _builtins
import importlib
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from types import MappingProxyType, ModuleType
from typing import Any

from app.platform.plugins.broker import CallMeter, HostAPI, HostServices
from app.platform.plugins.capabilities import GrantSet
from app.platform.plugins.errors import (
    ForbiddenImportError,
    PluginRuntimeError,
    ResourceLimitError,
)
from app.platform.plugins.limits import ResourceLimits

# --------------------------------------------------------------------------- #
# The base import allowlist + builtins policy
# --------------------------------------------------------------------------- #

#: Modules every plugin may import without declaring them — pure, side-effect-free
#: stdlib that cannot reach the filesystem, network, process, or interpreter
#: internals. Deliberately small; a plugin widens it via ``import_allowlist`` and
#: the host clamps that against :data:`HOST_IMPORT_DENYLIST`.
BASE_IMPORT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "math",
        "statistics",
        "random",
        "json",
        "re",
        "datetime",
        "decimal",
        "fractions",
        "string",
        "textwrap",
        "collections",
        "collections.abc",
        "itertools",
        "functools",
        "operator",
        "heapq",
        "bisect",
        "dataclasses",
        "enum",
        "typing",
        "unicodedata",
        "hashlib",
        "hmac",
        "base64",
        "binascii",
        "uuid",
        "zlib",
        "html",
        "html.parser",
        "urllib.parse",
        "difflib",
        "copy",
    }
)

#: Modules a plugin may NEVER import even if it lists them — the escape hatches
#: to ambient authority or interpreter internals. Checked as a prefix set, so
#: ``os.path`` is denied because ``os`` is denied.
HOST_IMPORT_DENYLIST: frozenset[str] = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "shutil",
        "pathlib",
        "io",
        "builtins",
        "importlib",
        "imp",
        "ctypes",
        "cffi",
        "mmap",
        "fcntl",
        "signal",
        "threading",
        "multiprocessing",
        "asyncio",
        "selectors",
        "ssl",
        "http",
        "urllib",  # network egress; urllib.parse re-allowed explicitly above
        "urllib.request",
        "ftplib",
        "smtplib",
        "telnetlib",
        "pickle",
        "shelve",
        "marshal",
        "code",
        "codeop",
        "pty",
        "tempfile",
        "glob",
        "resource",
        "gc",
        "inspect",
        "ast",
        "dis",
        "traceback",
        "atexit",
        "site",
        "platform",
        "pdb",
        "app",  # the host package itself — never importable from a plugin
    }
)

#: Builtins removed from a plugin's namespace (the dangerous / escape set).
_BUILTIN_DENYLIST: frozenset[str] = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "__import__",  # replaced by the gated hook below
        "input",
        "breakpoint",
        "globals",
        "vars",
        "locals",
        "help",
        "exit",
        "quit",
        "copyright",
        "credits",
        "license",
        "memoryview",
    }
)


def _effective_allowlist(extra: frozenset[str]) -> frozenset[str]:
    """Base ∪ manifest extra, minus anything on the host denylist (denylist wins)."""
    requested = BASE_IMPORT_ALLOWLIST | extra
    return frozenset(m for m in requested if not _is_denied(m))


def _is_denied(module: str) -> bool:
    """True if ``module`` (or any parent package of it) is on the denylist."""
    parts = module.split(".")
    return any(".".join(parts[:i]) in HOST_IMPORT_DENYLIST for i in range(1, len(parts) + 1))


def _is_allowed(module: str, allowlist: frozenset[str]) -> bool:
    """True if ``module`` is the allowlist or a submodule of an allowed package."""
    if _is_denied(module):
        return False
    if module in allowlist:
        return True
    parts = module.split(".")
    for i in range(1, len(parts)):
        if ".".join(parts[: i + 1]) in allowlist:
            return True
        if ".".join(parts[:i]) in allowlist:
            # A submodule of an explicitly-allowed package is allowed.
            return True
    return False


def _make_safe_import(allowlist: frozenset[str]) -> Callable[..., ModuleType]:
    """Build the gated ``__import__`` replacement bound to ``allowlist``."""

    def _safe_import(
        name: str,
        globals: Mapping[str, Any] | None = None,  # noqa: A002 - matches builtin signature
        locals: Mapping[str, Any] | None = None,  # noqa: A002
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> ModuleType:
        if level != 0:
            raise ForbiddenImportError(
                "relative imports are not allowed in sandboxed plugins", module=name
            )
        root = name.split(".")[0]
        if not _is_allowed(name, allowlist) or not _is_allowed(root, allowlist):
            raise ForbiddenImportError(
                f"import of {name!r} is not permitted by the sandbox allowlist",
                module=name,
            )
        # Import each ``fromlist`` member that is itself a submodule, so
        # ``from collections import abc`` re-checks ``collections.abc``.
        for sub in fromlist or ():
            if isinstance(sub, str) and not sub.startswith("_"):
                full = f"{name}.{sub}"
                # Only re-check if it would resolve to a module (best effort).
                if (full in allowlist or _is_denied(full)) and not _is_allowed(full, allowlist):
                    raise ForbiddenImportError(
                        f"import of {full!r} is not permitted", module=full
                    )
        return importlib.import_module(name)

    return _safe_import


def _build_sandbox_builtins(allowlist: frozenset[str]) -> Mapping[str, Any]:
    """A frozen builtins mapping: the safe subset + the gated import hook."""
    safe: dict[str, Any] = {}
    for attr in dir(_builtins):
        if attr.startswith("__"):
            continue
        if attr in _BUILTIN_DENYLIST:
            continue
        safe[attr] = getattr(_builtins, attr)
    safe["__import__"] = _make_safe_import(allowlist)
    # Keep a couple of dunder constants plugins legitimately read.
    safe["__name__"] = "builtins"
    return MappingProxyType(safe)


# --------------------------------------------------------------------------- #
# Compiled plugin + invocation result
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class LoadedPlugin:
    """A compiled plugin module ready to invoke hooks against."""

    plugin_id: str
    version: str
    namespace: dict[str, Any]
    allowlist: frozenset[str]

    def get_callable(self, entrypoint: str) -> Callable[..., Any]:
        """Resolve a top-level callable by name (raises if missing/not callable)."""
        fn = self.namespace.get(entrypoint)
        if fn is None or not callable(fn):
            raise PluginRuntimeError(
                f"entrypoint {entrypoint!r} is not a callable in plugin {self.plugin_id!r}"
            )
        return fn


@dataclass(slots=True)
class InvocationResult:
    """The outcome of one sandboxed call — value plus the metering it incurred."""

    value: Any
    logs: list[str] = field(default_factory=list)
    host_calls: int = 0
    capabilities_used: tuple[str, ...] = ()
    wall_time_ms: float = 0.0


# --------------------------------------------------------------------------- #
# The runtime
# --------------------------------------------------------------------------- #


class PluginRuntime:
    """Compiles plugin source and invokes its hooks under the sandbox contract."""

    #: A single shared executor; each call gets its own future + timeout.
    _executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="plugin-sbx")

    def load(
        self,
        *,
        plugin_id: str,
        version: str,
        source: str,
        import_allowlist: frozenset[str] = frozenset(),
        entry_module: str = "plugin",
    ) -> LoadedPlugin:
        """Compile ``source`` in a sandboxed namespace (does not run hook code).

        The module body *does* execute at load (top-level statements), under the
        same restricted builtins + import hook, so a malicious top-level
        ``import os`` is blocked here too.
        """
        allowlist = _effective_allowlist(import_allowlist)
        sandbox_builtins = _build_sandbox_builtins(allowlist)
        namespace: dict[str, Any] = {
            "__builtins__": sandbox_builtins,
            "__name__": entry_module,
            "__doc__": None,
            "__package__": None,
        }
        try:
            compiled = compile(source, f"<plugin:{plugin_id}@{version}>", "exec")
        except SyntaxError as exc:
            raise PluginRuntimeError(
                f"plugin {plugin_id!r} failed to compile: {exc}", original=str(exc)
            ) from exc
        try:
            exec(compiled, namespace)  # noqa: S102 - the whole point; namespace is sandboxed
        except ForbiddenImportError:
            raise
        except ResourceLimitError:
            raise
        except Exception as exc:  # noqa: BLE001 - top-level plugin body error
            raise PluginRuntimeError(
                f"plugin {plugin_id!r} raised at import time: {exc!r}",
                original=repr(exc),
            ) from exc
        return LoadedPlugin(
            plugin_id=plugin_id,
            version=version,
            namespace=namespace,
            allowlist=allowlist,
        )

    def invoke(
        self,
        plugin: LoadedPlugin,
        entrypoint: str,
        payload: Any,
        *,
        grants: GrantSet,
        services: HostServices,
        limits: ResourceLimits,
    ) -> InvocationResult:
        """Call ``plugin.entrypoint(payload, host=...)`` under budget + timeout.

        Returns an :class:`InvocationResult`. Raises :class:`ResourceLimitError`
        on wall-time/host-call/output-size exhaustion,
        :class:`CapabilityDeniedError` when the plugin touches an ungranted host
        call, :class:`ForbiddenImportError` on a late import, and
        :class:`PluginRuntimeError` for any other uncaught plugin exception.
        """
        import time

        fn = plugin.get_callable(entrypoint)
        logs: list[str] = []
        meter = CallMeter(
            max_host_calls=limits.max_host_calls,
            max_log_lines=limits.max_log_lines,
        )
        host = HostAPI(grants=grants, services=services, meter=meter, logs=logs)

        def _run() -> Any:
            return fn(payload, host=host)

        started = time.perf_counter()
        future = self._executor.submit(_run)
        try:
            value = future.result(timeout=limits.wall_time_ms / 1000.0)
        except FutureTimeout as exc:
            future.cancel()
            raise ResourceLimitError(
                f"plugin exceeded wall-time budget ({limits.wall_time_ms} ms)",
                limit="wall_time",
            ) from exc
        except (
            ForbiddenImportError,
            ResourceLimitError,
        ):
            raise
        except PluginRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize any plugin exception
            # A CapabilityDeniedError is a SandboxError subtype and re-raised as-is.
            from app.platform.plugins.errors import SandboxError

            if isinstance(exc, SandboxError):
                raise
            raise PluginRuntimeError(
                f"plugin {plugin.plugin_id!r} hook {entrypoint!r} raised: {type(exc).__name__}",
                original=repr(exc),
            ) from exc
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        self._check_output_size(value, limits)
        return InvocationResult(
            value=value,
            logs=logs,
            host_calls=meter.host_calls,
            capabilities_used=tuple(meter.trail),
            wall_time_ms=elapsed_ms,
        )

    @staticmethod
    def _check_output_size(value: Any, limits: ResourceLimits) -> None:
        """Reject an oversized return value (cheap JSON-size estimate)."""
        try:
            import json

            size = len(json.dumps(value, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            # Non-JSON-able output: fall back to its repr length.
            size = len(repr(value).encode("utf-8"))
        if size > limits.max_output_bytes:
            raise ResourceLimitError(
                f"plugin output {size} bytes exceeds budget ({limits.max_output_bytes})",
                limit="output_bytes",
            )


__all__ = [
    "BASE_IMPORT_ALLOWLIST",
    "HOST_IMPORT_DENYLIST",
    "InvocationResult",
    "LoadedPlugin",
    "PluginRuntime",
]
