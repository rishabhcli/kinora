"""Unit tests for the whole-book long-range continuity audit. Pure, no DB/ffmpeg."""

from __future__ import annotations

from app.render.book_continuity_audit import audit_book_continuity


class _FakeShot:
    def __init__(
        self,
        shot_id: str,
        beat_index: int,
        wardrobe: str | None = None,
        setting: str | None = None,
        lighting: str | None = None,
        time_of_day: str | None = None,
        hand_off: str = "",
        summary: str = "",
    ) -> None:
        self.shot_id = shot_id
        self.beat_index = beat_index
        self.wardrobe = wardrobe
        self.setting = setting
        self.lighting = lighting
        self.time_of_day = time_of_day
        self.hand_off = hand_off
        self.summary = summary


def test_no_drift_when_wardrobe_never_changes() -> None:
    shots = [
        _FakeShot("s1", 0, wardrobe="blue coat"),
        _FakeShot("s2", 5, wardrobe="blue coat"),
        _FakeShot("s3", 40, wardrobe="blue coat"),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert report.ok
    assert report.drifts == ()


def test_unmotivated_wardrobe_change_flagged_high_confidence() -> None:
    shots = [
        _FakeShot("s1", 0, wardrobe="blue coat"),
        _FakeShot("s2", 40, wardrobe="red coat", hand_off="", summary="she walks on"),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert not report.ok
    assert len(report.drifts) == 1
    drift = report.drifts[0]
    assert drift.dimension == "wardrobe"
    assert drift.from_value == "blue coat"
    assert drift.to_value == "red coat"


def test_motivated_wardrobe_change_not_flagged() -> None:
    shots = [
        _FakeShot("s1", 0, wardrobe="blue coat"),
        _FakeShot(
            "s2",
            40,
            wardrobe="red coat",
            hand_off="she changes into her red coat before the ball",
            summary="",
        ),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert report.ok


def test_unmotivated_setting_change_flagged() -> None:
    shots = [
        _FakeShot("s1", 0, setting="the tavern"),
        _FakeShot("s2", 40, setting="the docks", hand_off="", summary="she walks on"),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert not report.ok
    assert len(report.drifts) == 1
    drift = report.drifts[0]
    assert drift.dimension == "setting"
    assert drift.from_value == "the tavern"
    assert drift.to_value == "the docks"


def test_unmotivated_lighting_change_flagged() -> None:
    shots = [
        _FakeShot("s1", 0, lighting="warm candlelight"),
        _FakeShot("s2", 40, lighting="cold moonlight", hand_off="", summary="she walks on"),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert not report.ok
    assert len(report.drifts) == 1
    drift = report.drifts[0]
    assert drift.dimension == "lighting"
    assert drift.from_value == "warm candlelight"
    assert drift.to_value == "cold moonlight"


def test_unmotivated_time_of_day_change_flagged() -> None:
    shots = [
        _FakeShot("s1", 0, time_of_day="dusk"),
        _FakeShot("s2", 40, time_of_day="dawn", hand_off="", summary="she walks on"),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert not report.ok
    assert len(report.drifts) == 1
    drift = report.drifts[0]
    assert drift.dimension == "time_of_day"
    assert drift.from_value == "dusk"
    assert drift.to_value == "dawn"


def test_far_apart_change_after_fresh_establishing_shot_not_flagged() -> None:
    shots = [
        _FakeShot("s1", 0, wardrobe="blue coat"),
        _FakeShot(
            "s2",
            200,
            wardrobe="travelling cloak",
            summary="A new chapter opens, weeks later, in a different city.",
        ),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert report.ok  # a fresh establishing shot legitimately resets context


def test_far_apart_seasonal_time_passage_not_flagged() -> None:
    """Regression: classic-literature prose signals a time-skip indirectly
    ("Autumn had turned to winter...") far more often than with the blunt
    "weeks later"-style markers — this exact sentence used to slip past every
    cue and get flagged as unmotivated drift.
    """
    shots = [
        _FakeShot("s1", 0, wardrobe="summer dress"),
        _FakeShot(
            "s2",
            200,
            wardrobe="heavy travelling cloak",
            summary="Autumn had turned to winter by the time she reached the mountains.",
        ),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert report.ok


def test_far_apart_ordinary_continuation_still_flagged() -> None:
    """The expanded cue list must not overcorrect: a merely non-empty summary
    with no genuine time/place-reset language still counts as unmotivated.
    """
    shots = [
        _FakeShot("s1", 0, wardrobe="blue coat"),
        _FakeShot(
            "s2",
            200,
            wardrobe="red coat",
            summary="She walks on through the summer garden.",
        ),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert not report.ok


def test_gave_way_to_alone_does_not_excuse_unmotivated_drift() -> None:
    """Regression (independent review finding): "gave way to" describes an
    immediate emotional/physical transition within a single moment ("her fear
    gave way to anger"), not a scene/time reset — unlike every other cue,
    which names an explicit chapter/place/season change. It must not fully
    suppress a genuine continuity defect just because it happens to appear
    somewhere in the shot's text.
    """
    shots = [
        _FakeShot("s1", 0, wardrobe="blue coat"),
        _FakeShot(
            "s2",
            200,
            wardrobe="red coat",
            summary="Her fear gave way to a strange calm as she stepped through the door.",
        ),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert not report.ok
    assert report.drifts[0].confidence == "low"  # still far apart, just no longer excused


def test_by_the_time_alone_does_not_excuse_unmotivated_drift() -> None:
    """Regression (independent review finding): "by the time" is an ordinary
    subordinating construction ("by the time she finished her tea..."), not a
    reliable time-skip signal on its own — unlike "the following morning" or
    "turned to winter", which it used to ride along with in the cue list.
    """
    shots = [
        _FakeShot("s1", 0, wardrobe="blue coat"),
        _FakeShot(
            "s2",
            200,
            wardrobe="red coat",
            summary="By the time she finished her tea, the guests had already left.",
        ),
    ]
    report = audit_book_continuity("book1", shots, canon_snapshots_by_shot={})
    assert not report.ok
    assert report.drifts[0].confidence == "low"
