"""Exception hierarchy for the ML dataset + trace pipeline.

A single root (:class:`MLDataError`) so callers (the sibling alignment / serving
facets, the API layer) can catch the whole subsystem with one ``except`` while
still distinguishing the specific failure when they care. Every raise in
:mod:`app.mlplatform.datasets` uses one of these — never a bare ``ValueError`` —
so the boundary is crisp and greppable.
"""

from __future__ import annotations


class MLDataError(Exception):
    """Base class for every error raised by the dataset pipeline."""


class SourceError(MLDataError):
    """A :class:`TraceSource` could not yield records (bad cursor, malformed row)."""


class IngestError(MLDataError):
    """A raw record could not be normalized into a :class:`TraceExample`."""


class DatasetError(MLDataError):
    """A dataset is structurally invalid (empty, duplicate ids, bad schema)."""


class VersionError(MLDataError):
    """A dataset version / lineage operation is invalid (unknown id, cycle)."""


class ImmutabilityError(VersionError):
    """An attempt to mutate or overwrite a frozen, content-addressed version."""


class SplitError(MLDataError):
    """A train/val/test split is invalid (bad ratios, would leak across groups)."""


class LabelError(MLDataError):
    """A labeling / weak-supervision rule is malformed or conflicts irreconcilably."""


class ExportError(MLDataError):
    """An export adapter could not serialize a dataset to its target format."""


class ScrubError(MLDataError):
    """A PII scrub rule is malformed or a scrub invariant was violated."""


__all__ = [
    "DatasetError",
    "ExportError",
    "ImmutabilityError",
    "IngestError",
    "LabelError",
    "MLDataError",
    "ScrubError",
    "SourceError",
    "SplitError",
    "VersionError",
]
