"""Cross-session preference-learning acceptance test (kinora.md §8.6, §9.6).

The proof the goal asks for, with no DB or network: drive the *real*
``PrefsService`` (over an in-memory repo with the production read/nudge
semantics) and the *real* ``Cinematographer``. Three "slower" director notes in
"session 1" must make a freshly-designed shot in "session 2" default to slower
camera moves — without any note re-typed — and a reset must clear it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.cinematographer import Cinematographer
from app.agents.contracts import Beat
from app.db.models.pref import Pref
from app.memory.interfaces import CanonEntitySlice, CanonSlice, RefImage
from app.memory.prefs_service import PrefsService
from app.memory.prefs_signals import describe
from app.providers import Providers
from tests.test_agents_support import (
    JsonSequencer,
    providers,  # noqa: F401  (pytest fixture)
)


@dataclass
class FakePrefsRepo:
    """In-memory ``PrefsStore`` with the real get/upsert_nudge/delete semantics."""

    rows: list[Pref] = field(default_factory=list)

    async def get(
        self,
        *,
        user_id: str | None = None,
        book_id: str | None = None,
        kind: str | None = None,
    ) -> list[Pref]:
        return [
            r
            for r in self.rows
            if (user_id is None or r.user_id == user_id)
            and (book_id is None or r.book_id == book_id)
            and (kind is None or r.kind == kind)
        ]

    async def upsert_nudge(
        self,
        *,
        kind: str,
        value: dict[str, Any],
        user_id: str | None = None,
        book_id: str | None = None,
        weight_delta: float = 1.0,
    ) -> Pref:
        for r in self.rows:
            if r.user_id == user_id and r.book_id == book_id and r.kind == kind:
                r.value = value
                r.weight += weight_delta
                return r
        row = Pref(user_id=user_id, book_id=book_id, kind=kind, value=value, weight=weight_delta)
        self.rows.append(row)
        return row

    async def delete(self, *, user_id: str | None = None, book_id: str | None = None) -> int:
        keep = [
            r
            for r in self.rows
            if (user_id is not None and r.user_id != user_id)
            or (book_id is not None and r.book_id != book_id)
        ]
        removed = len(self.rows) - len(keep)
        self.rows = keep
        return removed


def _canon_with_locked_hero() -> CanonSlice:
    hero = CanonEntitySlice(
        entity_key="char_hero",
        type="character",
        name="Hero",
        version=2,
        reference_images=[RefImage(key="refs/hero/front.png", locked=True)],
        valid_from_beat=1,
    )
    return CanonSlice(
        book_id="b1",
        beat_id="beat_0007",
        beat_index=7,
        scene_id="scene_002",
        characters=[hero],
    )


#: The model's "neutral" fill — medium speed. The prior must be what makes it slow.
_NEUTRAL_FILL = {
    "prompt": "the hero crosses the courtyard",
    "negative_prompt": "extra fingers",
    "reference_image_ids": ["char_hero@v2"],
    "camera": {"move": "static", "speed": "medium", "shot_size": "medium"},
    "seed": 4242,
}


async def test_three_slower_notes_shift_next_session_default(providers: Providers) -> None:  # noqa: F811
    repo = FakePrefsRepo()
    prefs = PrefsService(prefs=repo)

    # --- Session 1: three "slower" Director notes on this book (user u1). ----- #
    for _ in range(3):
        learned = await prefs.record_note(
            "this is too fast — slow it down", user_id="u1", book_id="b1"
        )
        assert [p.kind for p in learned] == ["pacing"]

    priors = await prefs.get(book_id="b1")
    assert priors.priors["pacing"].value["bias"] == -0.9
    assert priors.priors["pacing"].weight == 3.0

    # The Settings panel renders the accumulated prior in plain language.
    label, _ = describe(priors.priors["pacing"])
    assert label == "You prefer slower, lingering shots"

    # --- Session 2: a NEW shot, no notes — the learned prior must apply. ------ #
    providers.chat.chat_json = JsonSequencer(_NEUTRAL_FILL)  # type: ignore[method-assign]
    cinematographer = Cinematographer(providers)
    beat = Beat(beat_id="beat_0007", scene_id="scene_002", summary="the hero crosses")
    canon = _canon_with_locked_hero()

    # Control: with no priors, the model's neutral medium speed stands.
    baseline = await cinematographer.design_shot(beat, canon, [], priors=None)
    assert baseline.camera.speed == "medium"

    # With the learned priors, the default shifts to slow — without any new note.
    personalized = await cinematographer.design_shot(beat, canon, [], priors=priors)
    assert personalized.camera.speed == "slow"

    # --- Reset clears it: the next design reverts to the neutral default. ----- #
    cleared = await prefs.reset(book_id="b1")
    assert cleared == 1
    after_reset = await prefs.get(book_id="b1")
    assert after_reset.priors == {}
    reverted = await cinematographer.design_shot(beat, canon, [], priors=after_reset)
    assert reverted.camera.speed == "medium"


async def test_explicit_in_session_note_overrides_learned_prior(providers: Providers) -> None:  # noqa: F811
    """A note that addresses pacing this session wins over the learned slow prior."""
    repo = FakePrefsRepo()
    prefs = PrefsService(prefs=repo)
    for _ in range(3):
        await prefs.record_note("slower", user_id="u1", book_id="b1")
    priors = await prefs.get(book_id="b1")

    providers.chat.chat_json = JsonSequencer(_NEUTRAL_FILL)  # type: ignore[method-assign]
    cinematographer = Cinematographer(providers)
    beat = Beat(beat_id="beat_0007", scene_id="scene_002", summary="the hero crosses")
    from app.agents.contracts import DirectorNote

    spec = await cinematographer.design_shot(
        beat,
        _canon_with_locked_hero(),
        [DirectorNote(note="make this faster")],
        priors=priors,
    )
    # The learned "slow" default did NOT override the explicit "faster" ask.
    assert spec.camera.speed != "slow"


async def test_repeated_wider_notes_default_to_wide_framing(providers: Providers) -> None:  # noqa: F811
    """The goal's "slower, wider": repeated framing notes shift the default shot size."""
    repo = FakePrefsRepo()
    prefs = PrefsService(prefs=repo)
    for _ in range(2):
        await prefs.record_note("pull back, show more of the scene", user_id="u1", book_id="b1")
    priors = await prefs.get(book_id="b1")

    providers.chat.chat_json = JsonSequencer(_NEUTRAL_FILL)  # type: ignore[method-assign]
    cinematographer = Cinematographer(providers)
    beat = Beat(beat_id="beat_0007", scene_id="scene_002", summary="the hero crosses")
    spec = await cinematographer.design_shot(beat, _canon_with_locked_hero(), [], priors=priors)
    assert spec.camera.shot_size == "wide"


async def test_opposing_notes_net_out(providers: Providers) -> None:  # noqa: F811
    repo = FakePrefsRepo()
    prefs = PrefsService(prefs=repo)
    await prefs.record_note("slower", user_id="u1", book_id="b1")
    await prefs.record_note("faster", user_id="u1", book_id="b1")
    priors = await prefs.get(book_id="b1")
    # Net bias is 0 → no applied default.
    assert priors.priors["pacing"].value["bias"] == 0.0


async def test_stale_prior_decays_below_apply_threshold(providers: Providers) -> None:  # noqa: F811
    """A taste you stop expressing fades (§8.5): 90-day-old 'slower' no longer applies."""
    from datetime import UTC, datetime, timedelta

    from app.memory.prefs_signals import is_applied

    repo = FakePrefsRepo()
    prefs = PrefsService(prefs=repo)
    for _ in range(3):
        await prefs.record_note("slower", user_id="u1", book_id="b1")

    # Fresh: applied (bias -0.9).
    fresh = await prefs.get(book_id="b1")
    assert is_applied(fresh.priors["pacing"]) is True

    # Age the single row 90 days (two 45-day half-lives → ×0.25).
    repo.rows[0].updated_at = datetime.now(UTC) - timedelta(days=90)
    stale = await prefs.get(book_id="b1")
    assert abs(stale.priors["pacing"].value["bias"] - (-0.225)) < 0.01
    assert is_applied(stale.priors["pacing"]) is False  # faded back under the bar


def test_pacing_drives_dwell_and_palette_lighting_drive_grade() -> None:
    """The learned look reaches the off-gate clip: pacing → dwell, palette/lighting → grade."""
    from app.agents.contracts import Camera
    from app.render.degrade import duration_for_pacing, grade_filter

    slow = duration_for_pacing(5.0, Camera(speed="slow"))
    fast = duration_for_pacing(5.0, Camera(speed="fast"))
    assert fast < 5.0 < slow  # slower lingers longer, faster tightens

    warm = grade_filter(palette="warm")
    assert warm and "colorbalance" in warm
    dark = grade_filter(lighting="dark")
    assert dark and "eq=" in dark
    both = grade_filter(palette="cool", lighting="bright")
    assert both and both.count(",") >= 1  # two filters joined
    assert grade_filter() is None  # nothing learned → no grade (unchanged look)


async def test_get_effective_blends_global_under_book(providers: Providers) -> None:  # noqa: F811
    """A new book inherits the reader's global taste; the book's own axes win (§8.6)."""
    repo = FakePrefsRepo()
    prefs = PrefsService(prefs=repo)
    # Reader taught pacing on book A, palette on book B.
    for _ in range(2):
        await prefs.record_note("slower", user_id="u1", book_id="bookA")
        await prefs.record_note("warmer", user_id="u1", book_id="bookB")

    eff = await prefs.get_effective(user_id="u1", book_id="bookA")
    # Book A's own axis (pacing) is present, AND the global palette (from book B)
    # fills the axis book A never learned.
    assert eff.priors["pacing"].value["bias"] < 0  # slower, from book A
    assert eff.priors["palette"].value["bias"] > 0  # warmer, inherited globally

    # A brand-new book C (no signals) is still directed in the reader's taste.
    eff_new = await prefs.get_effective(user_id="u1", book_id="bookC")
    assert {"pacing", "palette"} <= set(eff_new.priors)


def test_learned_camera_drives_ken_burns_zoom() -> None:
    """The prior reaches the *pixels*: camera speed/size modulate the off-gate
    Ken-Burns push, so a learned "slower / wider" taste is visible without live Wan."""
    from app.agents.contracts import Camera
    from app.render.degrade import zoom_for_camera

    slow = zoom_for_camera(Camera(speed="slow", shot_size="medium"))
    medium = zoom_for_camera(Camera(speed="medium", shot_size="medium"))
    fast = zoom_for_camera(Camera(speed="fast", shot_size="medium"))
    # Slower pacing = a gentler push; faster = more motion energy.
    assert slow < medium < fast

    wide = zoom_for_camera(Camera(speed="medium", shot_size="wide"))
    close = zoom_for_camera(Camera(speed="medium", shot_size="close"))
    # Wider framing keeps the frame calmer; closer pushes in harder.
    assert wide < medium < close

    # No camera info → the neutral default (unchanged behaviour for un-learned shots).
    assert zoom_for_camera(None) == medium
