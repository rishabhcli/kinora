"""Workflow versioning / patching — deploying new code against old histories.

The replay model has a sharp edge: if you change a workflow's code, replaying an
*in-flight* history against the new code can diverge (the new code emits a
different command than the recorded event) → a
:class:`~app.platform.workflows.errors.NonDeterminismError`. Versioning is the
escape hatch, mirroring Temporal's ``GetVersion`` / ``patched`` API.

The pattern:

* The **first time** a ``get_version(change_id, min, max)`` call executes in a
  *fresh* run, the engine records a ``VERSION_MARKER`` event pinning the chosen
  version (the max supported by the running code). New code branches on the
  returned version.
* On **replay**, the same call reads the pinned version back from history, so the
  workflow takes the branch it took originally — even if the code now supports a
  newer version. Old in-flight runs keep their old behaviour; new runs get new.
* Once every pre-change run has drained, the ``min`` can be raised and the old
  branch deleted in a later deploy.

:meth:`patched` is the boolean sugar over the integer version for the common
"add a new step" change: ``patched("add-fanout")`` returns True for new runs and
for old runs that recorded the patch, False for old runs that predate it.

This mixin is folded into :class:`~app.platform.workflows.context.WorkflowContext`;
it depends on the host providing ``_emit_version_marker`` and ``_recorded_version``.
"""

from __future__ import annotations

#: The version returned for a change that the *original* run never recorded — i.e.
#: code paths that existed before versioning was introduced for that change.
DEFAULT_VERSION = -1


class VersioningMixin:
    """Provides ``get_version`` / ``patched`` over host-supplied history hooks."""

    # Supplied by the concrete context.
    def _emit_version_marker(self, change_id: str, version: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def _recorded_version(self, change_id: str) -> int | None:  # pragma: no cover
        raise NotImplementedError

    def _reached_change_frontier(self) -> bool:  # pragma: no cover
        """True when this ``get_version`` is executing at the live frontier.

        False when the workflow is *replaying recorded history* that already moved
        past this code point — i.e. an old, in-flight run whose original code never
        recorded a marker here. Implemented by the concrete context.
        """
        raise NotImplementedError

    def get_version(self, change_id: str, min_supported: int, max_supported: int) -> int:
        """Return the pinned version for ``change_id`` within the supported range.

        Three cases, mirroring Temporal's ``GetVersion``:

        * **recorded marker present** → return it (validated against the range);
        * **no marker, at the live frontier** (a fresh execution of this code
          point) → pin and record ``max_supported``;
        * **no marker, replaying past-recorded history** (an old in-flight run that
          predates the change) → return ``DEFAULT_VERSION`` so the *old* branch
          runs, keeping the run deterministic. No marker is recorded for it.
        """
        if min_supported > max_supported:
            raise ValueError("min_supported must be <= max_supported")
        recorded = self._recorded_version(change_id)
        if recorded is None:
            if self._reached_change_frontier():
                self._emit_version_marker(change_id, max_supported)
                return max_supported
            return DEFAULT_VERSION
        if recorded < min_supported or recorded > max_supported:
            raise ValueError(
                f"version {recorded} for change {change_id!r} outside supported "
                f"range [{min_supported}, {max_supported}] — an old run reached code "
                "that no longer supports its pinned version"
            )
        return recorded

    def patched(self, change_id: str) -> bool:
        """Boolean sugar: True if the *new* branch should run for this execution."""
        version = self.get_version(change_id, DEFAULT_VERSION, 1)
        return version >= 1


__all__ = ["DEFAULT_VERSION", "VersioningMixin"]
