"""Plugin discovery — find candidate plugins from two sources, never crash.

A third party can install a Kinora video plugin two ways:

1. **A Python entry point** (``importlib.metadata``) under the
   :data:`ENTRY_POINT_GROUP` group — a ``pip install`` of their package makes the
   plugin discoverable with no Kinora config change. The entry point resolves to
   a callable/object exposing the manifest dict (or a ``MANIFEST`` attribute).
2. **A descriptor file** dropped in a plugins directory — a self-describing
   JSON manifest. This is the zero-packaging path (and the path the deterministic
   tests use, since it needs no installed distribution).

Discovery is *defensive by construction*: every source is parsed in isolation and
the two failure modes — a malformed descriptor and an **incompatible** plugin
(its ``kinora_api`` excludes this host) — are turned into a recorded
:class:`SkippedPlugin`, never an exception that aborts the whole sweep. A single
broken third-party plugin can therefore never stop the host from loading the good
ones. The result is a :class:`DiscoveryResult` carrying the discovered manifests,
the skips (with reasons), and the originating source descriptor for each.

Discovery never executes plugin code — it reads *data only* (the manifest). Code
runs later, behind the sandbox + conformance gate (see
:mod:`app.video.plugins.loader`).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.video.plugins.contracts import PLUGIN_API_VERSION
from app.video.plugins.errors import (
    DiscoveryError,
    IncompatiblePluginError,
    ManifestError,
)
from app.video.plugins.manifest import PluginManifest

logger = get_logger("app.video.plugins.discovery")

#: The entry-point group third-party distributions advertise plugins under.
ENTRY_POINT_GROUP = "kinora.video_plugins"
#: The descriptor filename suffix discovery looks for in a plugins directory.
DESCRIPTOR_SUFFIX = ".plugin.json"


@dataclass(frozen=True, slots=True)
class DiscoveredPlugin:
    """A successfully-parsed, host-compatible manifest plus where it came from."""

    manifest: PluginManifest
    source: str


@dataclass(frozen=True, slots=True)
class SkippedPlugin:
    """A discovery candidate that was skipped, with a machine-readable reason."""

    source: str
    reason_code: str
    detail: str
    plugin_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """The aggregate result of one discovery sweep."""

    discovered: tuple[DiscoveredPlugin, ...] = ()
    skipped: tuple[SkippedPlugin, ...] = ()

    def by_id(self) -> dict[str, DiscoveredPlugin]:
        return {d.manifest.id: d for d in self.discovered}


# --------------------------------------------------------------------------- #
# The discoverer
# --------------------------------------------------------------------------- #


class PluginDiscoverer:
    """Sweeps entry-points + directories for compatible plugin manifests."""

    def __init__(self, *, host_api: str = PLUGIN_API_VERSION) -> None:
        self._host_api = host_api

    # -- public sweep ----------------------------------------------------- #

    def discover(
        self,
        *,
        directories: Iterable[Path] | None = None,
        include_entry_points: bool = True,
        entry_points: Iterable[Any] | None = None,
    ) -> DiscoveryResult:
        """Discover from directories and/or entry points, accumulating skips.

        ``entry_points`` is an injection seam for tests (a list of objects with
        ``.name`` + ``.load()``); production leaves it ``None`` and reads the real
        ``importlib.metadata`` group.
        """
        discovered: list[DiscoveredPlugin] = []
        skipped: list[SkippedPlugin] = []

        for directory in directories or ():
            for d, s in self._scan_directory(Path(directory)):
                discovered.extend(d)
                skipped.extend(s)

        if include_entry_points:
            eps = entry_points if entry_points is not None else self._iter_entry_points()
            for d, s in self._scan_entry_points(eps):
                discovered.extend(d)
                skipped.extend(s)

        self._reject_duplicates(discovered, skipped)
        return DiscoveryResult(discovered=tuple(discovered), skipped=tuple(skipped))

    # -- directory source ------------------------------------------------- #

    def _scan_directory(
        self, directory: Path
    ) -> Iterator[tuple[list[DiscoveredPlugin], list[SkippedPlugin]]]:
        if not directory.is_dir():
            logger.debug("plugin_dir_missing", path=str(directory))
            return
        for path in sorted(directory.glob(f"*{DESCRIPTOR_SUFFIX}")):
            yield self._candidate_from_descriptor(path)

    def _candidate_from_descriptor(
        self, path: Path
    ) -> tuple[list[DiscoveredPlugin], list[SkippedPlugin]]:
        source = str(path)
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            return self._skip(source, DiscoveryError("descriptor unreadable/invalid JSON"), exc)
        return self._build_candidate(raw, source=source)

    # -- entry-point source ----------------------------------------------- #

    def _iter_entry_points(self) -> Iterable[Any]:  # pragma: no cover - reads real metadata
        try:
            eps = importlib_metadata.entry_points()
            selected = eps.select(group=ENTRY_POINT_GROUP)
        except Exception as exc:  # noqa: BLE001 - metadata is environment-dependent
            logger.warning("entry_point_enumeration_failed", error=repr(exc))
            return ()
        return list(selected)

    def _scan_entry_points(
        self, eps: Iterable[Any]
    ) -> Iterator[tuple[list[DiscoveredPlugin], list[SkippedPlugin]]]:
        for ep in eps:
            yield self._candidate_from_entry_point(ep)

    def _candidate_from_entry_point(
        self, ep: Any
    ) -> tuple[list[DiscoveredPlugin], list[SkippedPlugin]]:
        name = getattr(ep, "name", "<unknown>")
        source = f"entry-point:{name}"
        try:
            target = ep.load()
        except Exception as exc:  # noqa: BLE001 - third-party import can do anything
            return self._skip(source, DiscoveryError("entry point failed to load"), exc)
        raw = self._manifest_payload(target)
        if raw is None:
            return self._skip(
                source,
                DiscoveryError("entry point exposes no manifest (dict/MANIFEST/manifest())"),
                None,
            )
        return self._build_candidate(raw, source=source)

    @staticmethod
    def _manifest_payload(target: Any) -> dict[str, Any] | None:
        """Coerce an entry-point target to its manifest dict, if it has one."""
        if isinstance(target, dict):
            return target
        for attr in ("MANIFEST", "manifest"):
            value = getattr(target, attr, None)
            if isinstance(value, dict):
                return value
            if callable(value):
                try:
                    produced = value()
                except Exception:  # noqa: BLE001 - treat as no manifest
                    return None
                if isinstance(produced, dict):
                    return produced
        return None

    # -- shared candidate build ------------------------------------------- #

    def _build_candidate(
        self, raw: Any, *, source: str
    ) -> tuple[list[DiscoveredPlugin], list[SkippedPlugin]]:
        try:
            manifest = PluginManifest.parse(raw)
        except ManifestError as exc:
            return self._skip(source, exc, exc)
        if not manifest.is_compatible_with(self._host_api):
            inc = IncompatiblePluginError(
                f"plugin {manifest.id!r} targets kinora_api {manifest.kinora_api} "
                f"which excludes host {self._host_api}",
                plugin_id=manifest.id,
                required=str(manifest.kinora_api),
                host=self._host_api,
            )
            logger.info(
                "plugin_skipped_incompatible",
                plugin=manifest.ref,
                required=str(manifest.kinora_api),
                host=self._host_api,
            )
            return [], [
                SkippedPlugin(
                    source=source,
                    reason_code=inc.code,
                    detail=str(inc),
                    plugin_id=manifest.id,
                )
            ]
        return [DiscoveredPlugin(manifest=manifest, source=source)], []

    @staticmethod
    def _skip(
        source: str, err: Exception, cause: Exception | None
    ) -> tuple[list[DiscoveredPlugin], list[SkippedPlugin]]:
        code = getattr(err, "code", "video_plugin_discovery_failed")
        detail = f"{err}" if cause is None else f"{err}: {cause}"
        logger.warning("plugin_skipped", source=source, reason=code, detail=detail)
        return [], [SkippedPlugin(source=source, reason_code=code, detail=detail)]

    @staticmethod
    def _reject_duplicates(
        discovered: list[DiscoveredPlugin], skipped: list[SkippedPlugin]
    ) -> None:
        """Keep the highest version when the same id is discovered twice.

        Two sources advertising the same plugin id is normal (an editable
        descriptor shadowing an installed dist); the higher version wins and the
        loser is recorded as a skip so the precedence is observable.
        """
        best: dict[str, DiscoveredPlugin] = {}
        losers: list[DiscoveredPlugin] = []
        for cand in discovered:
            existing = best.get(cand.manifest.id)
            if existing is None:
                best[cand.manifest.id] = cand
            elif cand.manifest.version > existing.manifest.version:
                losers.append(existing)
                best[cand.manifest.id] = cand
            else:
                losers.append(cand)
        if not losers:
            return
        discovered[:] = [d for d in discovered if d not in losers]
        for loser in losers:
            skipped.append(
                SkippedPlugin(
                    source=loser.source,
                    reason_code="video_plugin_shadowed",
                    detail=(
                        f"{loser.manifest.ref} shadowed by a higher version "
                        f"of {loser.manifest.id}"
                    ),
                    plugin_id=loser.manifest.id,
                )
            )


__all__ = [
    "DESCRIPTOR_SUFFIX",
    "ENTRY_POINT_GROUP",
    "DiscoveredPlugin",
    "DiscoveryResult",
    "PluginDiscoverer",
    "SkippedPlugin",
]
