"""Consent-policy value objects + the default purpose catalog.

A :class:`PurposeSpec` describes one processing purpose Kinora asks consent for —
its title, whether granting it is mandatory, and which data class it unlocks. The
:data:`DEFAULT_PURPOSE_CATALOG` is the shipped baseline a deployment seeds; each
entry becomes a versioned :class:`~app.compliance.db.models.ConsentPolicy` row.

``body_hash`` pins the immutable text a subject agreed to, so a later edit of the
policy body forces a new version rather than silently changing what consent meant.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.compliance.enums import DataClass, ProcessingPurpose


def body_hash(body: str) -> str:
    """SHA-256 of a policy body, normalised (strip + collapse trailing whitespace)."""
    normalised = "\n".join(line.rstrip() for line in body.strip().splitlines())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PurposeSpec:
    """The shipped definition of one consent purpose."""

    purpose: ProcessingPurpose
    title: str
    body: str
    #: True == the product cannot run without it (e.g. the core adaptation).
    required: bool = False
    #: Data classes this consent is the lawful basis for (drives retention links).
    unlocks: tuple[DataClass, ...] = ()


@dataclass(frozen=True)
class PolicyDraft:
    """An author's request to publish a new policy version for a purpose."""

    purpose: ProcessingPurpose
    title: str
    body: str
    required: bool = False
    notes: dict[str, str] = field(default_factory=dict)


#: The shipped baseline catalog. A deployment publishes these as v1 policies on
#: first boot (see :meth:`ConsentService.seed_catalog`).
DEFAULT_PURPOSE_CATALOG: tuple[PurposeSpec, ...] = (
    PurposeSpec(
        purpose=ProcessingPurpose.ADAPTATION,
        title="Adapt your books into film",
        body=(
            "We read the books and PDFs you upload to generate a page-synced film "
            "(extracting text, characters, and scenes, and producing narration, "
            "keyframes, and short video clips). This is the core function of Kinora; "
            "without it the product cannot work."
        ),
        required=True,
        unlocks=(DataClass.UPLOADED_BOOK, DataClass.GENERATED_MEDIA),
    ),
    PurposeSpec(
        purpose=ProcessingPurpose.PERSONALIZATION,
        title="Learn your directing style",
        body=(
            "We remember the directing notes you give (pacing, palette, framing) so "
            "future shots default to your taste across sessions. You can clear this "
            "at any time from the Settings panel."
        ),
        unlocks=(DataClass.DIRECTING_PREFERENCE, DataClass.READING_SESSION),
    ),
    PurposeSpec(
        purpose=ProcessingPurpose.ANALYTICS,
        title="Improve Kinora with aggregate analytics",
        body=(
            "We collect de-identified, aggregated usage metrics (buffer occupancy, "
            "render latency, cache hit rates) to improve performance. No personal "
            "content is included in analytics."
        ),
    ),
    PurposeSpec(
        purpose=ProcessingPurpose.MODEL_TRAINING,
        title="Use your uploads to improve our models",
        body=(
            "With your permission, content you upload and the edits you make may be "
            "used to improve the generation models. This is OFF by default and never "
            "required to use Kinora."
        ),
    ),
    PurposeSpec(
        purpose=ProcessingPurpose.TRANSACTIONAL_EMAIL,
        title="Operational email",
        body=(
            "We email you operational notices: when a book finishes importing, when a "
            "data request is ready, and security alerts."
        ),
    ),
    PurposeSpec(
        purpose=ProcessingPurpose.MARKETING_EMAIL,
        title="Product news and offers",
        body=(
            "We email you product updates and occasional offers. You can unsubscribe "
            "at any time; this is always optional."
        ),
    ),
)


__all__ = [
    "DEFAULT_PURPOSE_CATALOG",
    "PolicyDraft",
    "PurposeSpec",
    "body_hash",
]
