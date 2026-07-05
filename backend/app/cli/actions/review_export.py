"""Local script+video review export — the human grading surface for a book.

Every earlier check (the ingest-time source-span reconciliation, the §9.5
Critic gate, the §7.2 conflict log) verifies "the right video at the right
point in the story" *mechanically*. This module produces the surface a human
uses to verify it *by eye*: a reading-order screenplay (``script.md``), a
structured ``manifest.json`` (mode, QA verdict/scores, defects per shot), the
actual rendered clips downloaded from object storage, and a static
``index.html`` viewer that plays each clip directly next to the narration text
and visual description it was generated from.

This is the tool the 10-book live-run grading pass uses: export a book, open
``index.html``, and confirm for each shot that the clip actually depicts what
the adjacent text says happens.
"""

from __future__ import annotations

import html as _html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from sqlalchemy import select

from app.cli.errors import not_found
from app.cli.output import Payload, kv_table
from app.composition import Container
from app.db.models.beat import Beat
from app.db.models.book import Book
from app.db.models.defect import Defect
from app.db.models.scene import Scene
from app.db.models.shot import Shot
from app.render.book_continuity_audit import LongRangeDrift, audit_book_continuity


@dataclass(frozen=True, slots=True)
class ReviewExportResult:
    """The result of ``books export-review``."""

    book_id: str
    title: str
    out_dir: str
    num_scenes: int
    num_beats: int
    num_shots: int
    num_clips_downloaded: int
    num_defects: int

    def render_payload(self) -> Payload:
        data = {
            "book_id": self.book_id,
            "title": self.title,
            "out_dir": self.out_dir,
            "num_scenes": self.num_scenes,
            "num_beats": self.num_beats,
            "num_shots": self.num_shots,
            "num_clips_downloaded": self.num_clips_downloaded,
            "num_defects": self.num_defects,
        }
        info = kv_table(
            "review export",
            {
                "book": f"{self.title} ({self.book_id})",
                "out_dir": self.out_dir,
                "scenes": self.num_scenes,
                "beats": self.num_beats,
                "shots": self.num_shots,
                "clips_downloaded": self.num_clips_downloaded,
                "defects": self.num_defects,
            },
        )
        return Payload.of(data, info)


def _shot_sort_key(shot: Shot, beat_order: dict[str, int]) -> tuple[int, int, str]:
    """Reading order: by the shot's beat ordinal, then its word-range start."""
    span = shot.source_span or {}
    word_range = span.get("word_range")
    start = 0
    if isinstance(word_range, (list, tuple)) and word_range:
        start = int(word_range[0])
    return (beat_order.get(shot.beat_id or "", 0), start, shot.id)


@dataclass(slots=True)
class _ContinuityShotView:
    """Adapts a persisted ``(Shot, Beat)`` pair to ``book_continuity_audit``'s
    ``ShotLike`` protocol, so the whole-book long-range continuity audit (§9.6,
    Task 4) can run against real DB rows instead of only the in-memory planning
    objects it was originally designed against.

    Deliberately NOT frozen: ``ShotLike``'s plain (non-property) attribute
    declarations make it a read-write structural protocol, so mypy requires an
    assignable attribute of matching type to consider this class compatible —
    a frozen dataclass's read-only attributes fail that check even though
    ``audit_book_continuity`` only ever reads them.
    """

    shot_id: str
    beat_index: int
    wardrobe: str | None
    setting: str | None
    lighting: str | None
    time_of_day: str | None
    hand_off: str
    summary: str


def _shot_continuity_view(shot: Shot, beat: Beat) -> _ContinuityShotView:
    """Build a ``ShotLike`` view from a shot's persisted ``continuity_directive``.

    ``continuity_directive`` is only populated for shots planned through the
    event-granularity path (a snapshot of that shot's planning-time
    ``ContinuityDirective``, event_director.py §9.6); for the default
    single-shot path it is ``None`` today, in which case every dimension below
    reads back unset and the audit simply has nothing to compare for this shot
    — not a crash, not a false positive.
    """
    directive = shot.continuity_directive or {}
    return _ContinuityShotView(
        shot_id=shot.id,
        beat_index=beat.beat_index,
        wardrobe=directive.get("wardrobe"),
        setting=directive.get("setting"),
        lighting=directive.get("lighting"),
        time_of_day=directive.get("time_of_day"),
        hand_off=directive.get("hand_off", ""),
        summary=beat.summary or "",
    )


def _describe_drift(drift: LongRangeDrift) -> dict[str, Any]:
    """Render one ``LongRangeDrift`` as a manifest-friendly dict (kept
    structured — not just ``.describe()``'s prose — so the HTML viewer and the
    cross-book aggregator can group/count by dimension or confidence later)."""
    return {
        "from_shot_id": drift.from_shot_id,
        "to_shot_id": drift.to_shot_id,
        "dimension": drift.dimension,
        "from_value": drift.from_value,
        "to_value": drift.to_value,
        "confidence": drift.confidence,
        "description": drift.describe(),
    }


async def export_book_review(
    container: Container,
    book_id: str,
    out_dir: str,
    *,
    max_shots: int | None = None,
) -> ReviewExportResult:
    """Export a reading-order script + downloaded clips + a static HTML viewer.

    Args:
        container: the wired composition container (DB + object storage).
        book_id: the book to export.
        out_dir: local directory to write ``script.md``/``manifest.json``/
            ``index.html``/``clips/*.mp4`` into (created if missing).
        max_shots: optional cap on how many shots (in reading order) to export
            — useful for a quick spot-check without downloading a whole book's
            clips.
    """
    async with container.session_factory() as db:
        book = await db.get(Book, book_id)
        if book is None:
            raise not_found("book", book_id)

        scenes = list(
            (
                await db.execute(
                    select(Scene).where(Scene.book_id == book_id).order_by(Scene.scene_index)
                )
            )
            .scalars()
            .all()
        )
        beats = list(
            (
                await db.execute(
                    select(Beat).where(Beat.book_id == book_id).order_by(Beat.beat_index)
                )
            )
            .scalars()
            .all()
        )
        shots = list(
            (await db.execute(select(Shot).where(Shot.book_id == book_id))).scalars().all()
        )
        defects = list(
            (
                await db.execute(
                    select(Defect)
                    .where(Defect.book_id == book_id)
                    .order_by(Defect.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

    beat_by_id = {b.id: b for b in beats}
    beat_order = {b.id: b.beat_index for b in beats}
    scene_by_id = {s.id: s for s in scenes}
    defects_by_shot: dict[str, list[Defect]] = {}
    # Newest-first (the query above orders by created_at desc), so the first
    # seam_repair defect found per shot below is that shot's MOST RECENT
    # repair action — the one that reflects what actually shipped.
    seam_repair_by_shot: dict[str, str] = {}
    for defect in defects:
        if defect.shot_id:
            defects_by_shot.setdefault(defect.shot_id, []).append(defect)
            if defect.kind == "seam_repair" and defect.shot_id not in seam_repair_by_shot:
                action = (defect.detail or {}).get("action")
                if action:
                    seam_repair_by_shot[defect.shot_id] = action

    ordered_shots = sorted(shots, key=lambda s: _shot_sort_key(s, beat_order))
    if max_shots is not None:
        ordered_shots = ordered_shots[:max_shots]

    root = Path(out_dir)
    (root / "clips").mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    clips_downloaded = 0
    for shot in ordered_shots:
        beat = beat_by_id.get(shot.beat_id or "")
        scene = scene_by_id.get(shot.scene_id or "") if shot.scene_id else None
        narration = (shot.narration or {}).get("text") or (beat.summary if beat else "") or ""
        output = shot.output or {}
        clip_key = output.get("clip_key")
        clip_rel: str | None = None
        if clip_key:
            data_bytes: bytes | None
            try:
                data_bytes = await anyio.to_thread.run_sync(
                    container.object_store.get_bytes, clip_key
                )
            except Exception:  # noqa: BLE001 - a missing/expired object must not abort export
                data_bytes = None
            if data_bytes is not None:
                clip_rel = f"clips/{shot.id}.mp4"
                (root / clip_rel).write_bytes(data_bytes)
                clips_downloaded += 1

        qa = shot.qa or {}
        entries.append(
            {
                "shot_id": shot.id,
                "scene_index": scene.scene_index if scene else None,
                "beat_index": beat.beat_index if beat else None,
                "page": (shot.source_span or {}).get("page"),
                "word_range": (shot.source_span or {}).get("word_range"),
                "status": shot.status.value,
                "render_mode": shot.render_mode,
                "prompt": shot.prompt,
                "narration_text": narration,
                "described_visuals": beat.described_visuals if beat else None,
                "mood": beat.mood if beat else None,
                "qa": shot.qa,
                "qa_ccs": qa.get("ccs"),
                "qa_style_drift": qa.get("style_drift"),
                "seam_repair_action": seam_repair_by_shot.get(shot.id),
                "clip_key": clip_key,
                "clip_file": clip_rel,
                "defects": [
                    {"kind": d.kind, "detail": d.detail} for d in defects_by_shot.get(shot.id, [])
                ],
            }
        )

    # Whole-book long-range continuity audit (Task 4, §9.6): walk the same
    # reading-order shot list through the (Shot, Beat) -> ShotLike adapter and
    # flag unmotivated persistence drift a human reviewer should double check.
    # Shots with no resolvable beat are skipped — beat_index (the audit's
    # ordering key) has no meaning for them.
    adapted_shots = [
        _shot_continuity_view(shot, beat)
        for shot in ordered_shots
        if (beat := beat_by_id.get(shot.beat_id or "")) is not None
    ]
    continuity_report = audit_book_continuity(book_id, adapted_shots)
    long_range_findings = [_describe_drift(d) for d in continuity_report.drifts]

    manifest = {
        "book_id": book_id,
        "title": book.title,
        "author": book.author,
        "exported_shots": len(entries),
        "shots": entries,
        "long_range_findings": long_range_findings,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    (root / "script.md").write_text(_render_script_markdown(book, entries))
    (root / "index.html").write_text(_render_html_viewer(book, entries, long_range_findings))

    return ReviewExportResult(
        book_id=book_id,
        title=book.title,
        out_dir=str(root),
        num_scenes=len(scenes),
        num_beats=len(beats),
        num_shots=len(ordered_shots),
        num_clips_downloaded=clips_downloaded,
        num_defects=len(defects),
    )


def _render_script_markdown(book: Book, entries: list[dict[str, Any]]) -> str:
    lines = [f"# {book.title}", ""]
    if book.author:
        lines.append(f"*by {book.author}*")
        lines.append("")
    current_scene: int | None = None
    for entry in entries:
        if entry["scene_index"] != current_scene:
            current_scene = entry["scene_index"]
            lines.append(f"\n## Scene {current_scene}\n")
        qa = entry["qa"] or {}
        verdict = qa.get("verdict", "-")
        lines.append(
            f"**Shot `{entry['shot_id']}`** — page {entry['page']}, "
            f"{entry['render_mode'] or 'planned'}, status `{entry['status']}`, QA `{verdict}`"
        )
        if entry["narration_text"]:
            lines.append(f"> {entry['narration_text']}")
        if entry["described_visuals"]:
            lines.append(f"- visual: {entry['described_visuals']}")
        if entry["clip_file"]:
            lines.append(f"- clip: `{entry['clip_file']}`")
        for defect in entry["defects"]:
            lines.append(f"- ⚠ defect: {defect['kind']} — {defect['detail']}")
        lines.append("")
    return "\n".join(lines)


def _render_html_viewer(
    book: Book,
    entries: list[dict[str, Any]],
    long_range_findings: list[dict[str, Any]] | None = None,
) -> str:
    rows: list[str] = []
    current_scene: int | None = None
    for entry in entries:
        if entry["scene_index"] != current_scene:
            current_scene = entry["scene_index"]
            rows.append(f"<h2>Scene {current_scene}</h2>")
        qa = entry["qa"] or {}
        verdict = str(qa.get("verdict") or "-")
        badge_class = "pass" if verdict == "pass" else "fail" if verdict == "fail" else "none"
        clip_file = entry["clip_file"]
        video_tag = (
            f'<video controls preload="none" src="{_html.escape(clip_file)}"></video>'
            if clip_file
            else '<div class="no-clip">no clip yet</div>'
        )
        ccs = entry.get("qa_ccs")
        style_drift = entry.get("qa_style_drift")
        ccs_text = f"{ccs:.2f}" if isinstance(ccs, (int, float)) else "-"
        drift_text = f"{style_drift:.2f}" if isinstance(style_drift, (int, float)) else "-"
        scores_html = (
            f'&middot; <span class="scores">ccs {_html.escape(ccs_text)} '
            f"&middot; drift {_html.escape(drift_text)}</span>"
        )
        repair_action = entry.get("seam_repair_action")
        repair_html = (
            f'<div class="repair">repair action: {_html.escape(str(repair_action))}</div>'
            if repair_action
            else ""
        )
        defect_html = "".join(
            f'<div class="defect">⚠ {_html.escape(d["kind"])}</div>'
            for d in entry["defects"]
        )
        rows.append(
            f"""
        <div class="shot">
          <div class="video">{video_tag}</div>
          <div class="text">
            <div class="meta">shot {_html.escape(entry['shot_id'])}
              &middot; page {_html.escape(str(entry['page']))}
              &middot; {_html.escape(entry['render_mode'] or 'planned')}
              &middot; status {_html.escape(entry['status'])}
              &middot; <span class="badge {badge_class}">QA: {_html.escape(verdict)}</span>
              {scores_html}
            </div>
            <p class="narration">{_html.escape(entry['narration_text'] or '')}</p>
            <p class="visual"><em>{_html.escape(entry['described_visuals'] or '')}</em></p>
            {repair_html}
            {defect_html}
          </div>
        </div>
        """
        )
    body = "\n".join(rows)
    findings_html = ""
    if long_range_findings:
        items = "".join(
            f'<li><strong>{_html.escape(f["dimension"])}</strong>: '
            f'{_html.escape(str(f["description"]))}</li>'
            for f in long_range_findings
        )
        findings_html = f"""
    <div class="findings">
      <h2>Long-range continuity findings ({len(long_range_findings)})</h2>
      <ul>{items}</ul>
    </div>
        """
    title = _html.escape(book.title)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title} — review</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 900px;
  margin: 2rem auto; padding: 0 1rem; background: #0b0b0f; color: #eee; }}
h1, h2 {{ font-weight: 600; }}
.shot {{ display: flex; gap: 1.5rem; margin-bottom: 2rem; padding-bottom: 1.5rem;
  border-bottom: 1px solid #333; align-items: flex-start; }}
video {{ width: 320px; border-radius: 8px; background: #000; }}
.no-clip {{ width: 320px; height: 180px; display: flex; align-items: center;
  justify-content: center; background: #1a1a1f; border-radius: 8px; color: #777; }}
.text {{ flex: 1; }}
.meta {{ font-size: 0.8rem; color: #999; margin-bottom: 0.5rem; }}
.badge {{ padding: 0.1rem 0.5rem; border-radius: 4px; font-weight: 600; }}
.badge.pass {{ background: #1d3b25; color: #6ee787; }}
.badge.fail {{ background: #3b1d1d; color: #e78787; }}
.badge.none {{ background: #333; color: #aaa; }}
.narration {{ font-size: 1.05rem; line-height: 1.5; }}
.visual {{ color: #aaa; }}
.defect {{ color: #e78787; font-size: 0.85rem; margin-top: 0.4rem; }}
.scores {{ color: #8ab4f8; }}
.repair {{ color: #f0c674; font-size: 0.85rem; margin-top: 0.4rem; }}
.findings {{ margin-bottom: 2rem; padding: 1rem; background: #1a1420;
  border: 1px solid #4a2f5f; border-radius: 8px; }}
.findings h2 {{ margin-top: 0; color: #d8b4fe; }}
.findings li {{ margin-bottom: 0.4rem; }}
</style>
</head>
<body>
<h1>{title}</h1>
{findings_html}
{body}
</body></html>"""


__all__ = ["ReviewExportResult", "export_book_review"]
