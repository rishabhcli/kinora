"""Thematic-motif planning and callbacks across a series (§7).

A *motif* is a recurring image/idea (the lost sword, the frozen window, the
refrain "the night is darkest before dawn"). Good series **plant** a motif early,
**echo** it through the middle, and **pay it off** near a climax. The Showrunner
plans those recurrences deterministically so the Cinematographer can lean into a
motif when a beat lands on a scheduled callback (a planted image returns in the
composition), and so the eval harness can check that every plant pays off.

Pure functions over :class:`~app.agents.contracts.Motif` /
:class:`~app.agents.contracts.MotifCallback`.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.agents.contracts import Motif, MotifCallback, MotifKind


def plan_motif_callbacks(
    motif: Motif,
    *,
    volume_beat_counts: dict[int, int],
    echoes_per_volume: int = 1,
) -> list[MotifCallback]:
    """Schedule a motif's plant / echo / payoff recurrences across volumes (§7).

    * One ``plant`` at the motif's declared planted position.
    * ``echoes_per_volume`` ``echo`` callbacks spread evenly through each volume
      between the plant and the first payoff (the motif stays alive in the reader's
      mind).
    * One ``payoff`` near the end of each volume in ``payoff_volumes`` (the image
      returns transformed at the climax).

    ``volume_beat_counts`` maps a volume index to its beat count so echo/payoff
    positions are placed proportionally. Callbacks are returned in series order.
    """
    callbacks: list[MotifCallback] = [
        MotifCallback(
            motif_id=motif.motif_id,
            kind=MotifKind.PLANT,
            volume_index=motif.planted_volume,
            beat_index=motif.planted_beat,
            note=f"plant: {motif.label or motif.motif_id}",
        )
    ]

    payoff_volumes = sorted(set(motif.payoff_volumes))
    last_payoff_vol = payoff_volumes[-1] if payoff_volumes else motif.planted_volume

    # Echoes: in every volume from the plant up to (but not including) the last
    # payoff volume, spread evenly. Skip the plant's exact beat.
    for vol in range(motif.planted_volume, last_payoff_vol):
        beats = max(1, volume_beat_counts.get(vol, 0))
        for k in range(1, echoes_per_volume + 1):
            beat = int(beats * k / (echoes_per_volume + 1))
            if vol == motif.planted_volume and beat <= motif.planted_beat:
                beat = motif.planted_beat + 1
            callbacks.append(
                MotifCallback(
                    motif_id=motif.motif_id,
                    kind=MotifKind.ECHO,
                    volume_index=vol,
                    beat_index=beat,
                    note=f"echo: {motif.label or motif.motif_id}",
                )
            )

    for vol in payoff_volumes:
        beats = max(1, volume_beat_counts.get(vol, 0))
        # Payoff lands in the final ~10% of the volume (near the climax).
        beat = max(0, beats - 1 - max(0, beats // 10))
        callbacks.append(
            MotifCallback(
                motif_id=motif.motif_id,
                kind=MotifKind.PAYOFF,
                volume_index=vol,
                beat_index=beat,
                note=f"payoff: {motif.label or motif.motif_id}",
            )
        )

    callbacks.sort(key=lambda c: (c.volume_index, c.beat_index))
    return callbacks


def plan_all_callbacks(
    motifs: Iterable[Motif],
    *,
    volume_beat_counts: dict[int, int],
    echoes_per_volume: int = 1,
) -> list[MotifCallback]:
    """Schedule every motif's callbacks, merged into one series-ordered list."""
    out: list[MotifCallback] = []
    for motif in motifs:
        out.extend(
            plan_motif_callbacks(
                motif,
                volume_beat_counts=volume_beat_counts,
                echoes_per_volume=echoes_per_volume,
            )
        )
    out.sort(key=lambda c: (c.volume_index, c.beat_index, c.motif_id))
    return out


def due_callbacks(
    callbacks: Iterable[MotifCallback],
    *,
    volume_index: int,
    beat_index: int,
    window: int = 0,
) -> list[MotifCallback]:
    """The callbacks that should fire at (or within ``window`` beats of) a position.

    The Cinematographer consults this for the current beat: if a motif callback is
    due, the shot's composition should reference the motif (the planted image
    returns). ``window`` widens the match so a callback near the beat still fires.
    """
    out: list[MotifCallback] = []
    for cb in callbacks:
        if cb.volume_index != volume_index:
            continue
        if abs(cb.beat_index - beat_index) <= window:
            out.append(cb)
    out.sort(key=lambda c: (abs(c.beat_index - beat_index), c.motif_id))
    return out


def motif_is_resolved(
    motif: Motif, callbacks: Iterable[MotifCallback]
) -> bool:
    """True iff the motif has at least one scheduled ``payoff`` callback (§7)."""
    return any(
        cb.motif_id == motif.motif_id and cb.kind is MotifKind.PAYOFF
        for cb in callbacks
    )


def unresolved_motifs(
    motifs: Iterable[Motif], callbacks: Iterable[MotifCallback]
) -> list[str]:
    """The motif ids that are planted but never pay off (a §13 eval signal)."""
    cb_list = list(callbacks)
    return [m.motif_id for m in motifs if not motif_is_resolved(m, cb_list)]


__all__ = [
    "due_callbacks",
    "motif_is_resolved",
    "plan_all_callbacks",
    "plan_motif_callbacks",
    "unresolved_motifs",
]
