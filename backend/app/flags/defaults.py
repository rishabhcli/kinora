"""Canonical Kinora flags + experiments — the platform applied to the product.

These are concrete, documented seeds that show how the abstract platform maps
onto Kinora's real decisions. They are *definitions only* — nothing here turns
anything on by itself; an operator (or the seed script) persists them and flips
the switches. The flag keys are referenced by name from product code via the
SDK, e.g. ``client.bool_variation(LIVE_VIDEO, ctx)``.

* :data:`LIVE_VIDEO` — the §-critical ``KINORA_LIVE_VIDEO`` gate, expressed as a
  flag so it can be flipped per-tenant / gradually rolled out instead of a global
  env var. **Defaults OFF** to honor the zero-credit rule.
* :data:`RENDER_LADDER` — which degradation-lane representation to default to
  (full video / cheap animatic / Ken-Burns still); a multivariate flag with a
  fast-skimmer rule that forces Ken-Burns (§4.4).
* :data:`LOOKAHEAD_SHOTS` — how many committed shots to keep ahead of the reader
  (a number flag the scheduler can read as a tuning knob, §4.10).
* :data:`CREW_VS_BASELINE` — the §13 experiment: control = single-agent (no
  memory), treatment = the six-agent crew + canon, with CCS as the primary metric
  and regen-rate as a guardrail.
* :data:`WATERMARK_BAND` — the §18-Q4 watermark-tuning A/B (standard vs. wider
  band) so the L/H/C choice is settled against the buffer sawtooth, not by feel.
"""

from __future__ import annotations

from app.flags.experiment import (
    Experiment,
    ExperimentStatus,
    Metric,
    MetricDirection,
    MetricKind,
    Variant,
)
from app.flags.models import (
    Clause,
    Flag,
    FlagKind,
    Operator,
    Rollout,
    Rule,
    Variation,
)

# --- flag keys (the stable names product code references) ------------------ #
LIVE_VIDEO = "live-video"
RENDER_LADDER = "render-ladder"
LOOKAHEAD_SHOTS = "lookahead-shots"
AGENT_FEED = "agent-activity-feed"
MANGA_MODE = "manga-overlay-mode"

# --- experiment keys ------------------------------------------------------- #
CREW_VS_BASELINE = "crew-vs-baseline"
WATERMARK_BAND = "watermark-band"


def live_video_flag() -> Flag:
    """The live-video go-live gate as a flag (defaults OFF; zero credits)."""
    return Flag.boolean(
        LIVE_VIDEO,
        enabled=False,
        default=False,
        name="Live Wan video",
        description=(
            "Master gate for spending real Wan video-seconds. OFF by default; "
            "the whole loop still runs on Ken-Burns mp4s with the budget at 0."
        ),
        tags=("budget", "render", "kill-switch"),
    )


def render_ladder_flag() -> Flag:
    """The §4.4 degradation-lane selector (full / animatic / kenburns)."""
    return Flag(
        key=RENDER_LADDER,
        kind=FlagKind.STRING,
        variations=(
            Variation("full", "full", name="Full reference/FLF video"),
            Variation("animatic", "animatic", name="Cheap 2s low-res animatic"),
            Variation("kenburns", "kenburns", name="Ken-Burns pan over a still"),
        ),
        default_variation="kenburns",
        # A fast skimmer (flipping faster than any pipeline can render) gets the
        # honest Ken-Burns answer (§4.1) regardless of the global default.
        rules=(
            Rule(
                "fast-skimmer",
                (Clause("velocity_wps", Operator.GT, (8,)),),
                variation="kenburns",
                description="skimmers aren't studying the animation — pan a still",
            ),
        ),
        fallthrough=Rollout.single("full"),
        name="Render ladder default",
        description="which representation a shot defaults to under the §4.4 ladder",
        tags=("render", "perf"),
    )


def lookahead_shots_flag() -> Flag:
    """How many committed shots to keep ahead of the reader (a tuning knob)."""
    return Flag(
        key=LOOKAHEAD_SHOTS,
        kind=FlagKind.NUMBER,
        variations=(
            Variation("conservative", 2),
            Variation("default", 3),
            Variation("aggressive", 5),
        ),
        default_variation="default",
        fallthrough=Rollout.single("default"),
        name="Committed look-ahead depth",
        description="committed shots kept ahead of the playhead (§4.10)",
        tags=("scheduler",),
    )


def agent_feed_flag(*, rollout_percent: float = 0.0) -> Flag:
    """The live agent-activity feed (a stretch feature; gradual rollout)."""
    return Flag.boolean(
        AGENT_FEED,
        enabled=True,
        rollout_percent=rollout_percent,
        name="Live agent-activity feed",
        description="surfaces the §7.2 crew negotiation live (stretch feature)",
        tags=("ui", "stretch"),
    )


def manga_mode_flag() -> Flag:
    """Manga/webtoon overlay mode (vision-only by default → disabled)."""
    return Flag.boolean(
        MANGA_MODE,
        enabled=False,
        name="Manga overlay mode",
        description="animated panel adaptation overlay (§18 Q7; vision-only)",
        tags=("ui", "vision"),
    )


def crew_vs_baseline_experiment(*, status: ExperimentStatus = ExperimentStatus.DRAFT) -> Experiment:
    """The §13 crew-vs-baseline study (control = single-agent, treatment = crew).

    Bucketed by ``book`` so an entire book is adapted under one arm (a fair
    per-book CCS comparison). Primary metric = character-consistency pass rate;
    guardrail = regeneration rate (lower is better) so a crew that "wins" CCS by
    regenerating endlessly is caught.
    """
    return Experiment(
        key=CREW_VS_BASELINE,
        variants=(
            Variant("baseline", 5000, is_control=True, flag_variation="single-agent"),
            Variant("crew", 5000, flag_variation="crew"),
        ),
        salt="ccs-2026",
        status=status,
        bucket_by="book",
        metrics=(
            Metric(
                "ccs_pass",
                kind=MetricKind.PROPORTION,
                direction=MetricDirection.INCREASE,
                name="Character-consistency pass rate",
            ),
            Metric(
                "regen_rate",
                kind=MetricKind.PROPORTION,
                direction=MetricDirection.DECREASE,
                is_guardrail=True,
                guardrail_margin=0.10,
                name="Regeneration rate",
            ),
        ),
        name="Crew + memory vs. single-agent baseline",
        description="kinora.md §13 — consistency-as-memory, measured",
    )


def watermark_band_experiment(*, status: ExperimentStatus = ExperimentStatus.DRAFT) -> Experiment:
    """The §18-Q4 watermark-band A/B (standard L=25/H=75 vs. a wider band)."""
    return Experiment(
        key=WATERMARK_BAND,
        variants=(
            Variant("standard", 5000, is_control=True, flag_variation="L25H75"),
            Variant("wide", 5000, flag_variation="L40H120"),
        ),
        salt="watermark-2026",
        status=status,
        bucket_by="session",
        metrics=(
            Metric(
                "no_stall",
                kind=MetricKind.PROPORTION,
                direction=MetricDirection.INCREASE,
                name="Stall-free reading fraction",
            ),
        ),
        name="Watermark band tuning",
        description="settle L/H/C against the buffer sawtooth (§18 Q4)",
    )


def default_flags() -> tuple[Flag, ...]:
    """Every canonical flag definition (for the seed script / admin bootstrap)."""
    return (
        live_video_flag(),
        render_ladder_flag(),
        lookahead_shots_flag(),
        agent_feed_flag(),
        manga_mode_flag(),
    )


def default_experiments() -> tuple[Experiment, ...]:
    """Every canonical experiment definition (drafts; an operator runs them)."""
    return (crew_vs_baseline_experiment(), watermark_band_experiment())


# Provide the watermark variation values the WATERMARK_BAND arms map to, so the
# scheduler can resolve an arm's flag_variation to concrete L/H/C numbers.
WATERMARK_BANDS: dict[str, dict[str, float]] = {
    "L25H75": {"low_s": 25.0, "high_s": 75.0, "commit_s": 45.0},
    "L40H120": {"low_s": 40.0, "high_s": 120.0, "commit_s": 70.0},
}


__all__ = [
    "AGENT_FEED",
    "CREW_VS_BASELINE",
    "LIVE_VIDEO",
    "LOOKAHEAD_SHOTS",
    "MANGA_MODE",
    "RENDER_LADDER",
    "WATERMARK_BAND",
    "WATERMARK_BANDS",
    "agent_feed_flag",
    "crew_vs_baseline_experiment",
    "default_experiments",
    "default_flags",
    "live_video_flag",
    "lookahead_shots_flag",
    "manga_mode_flag",
    "render_ladder_flag",
    "watermark_band_experiment",
]
