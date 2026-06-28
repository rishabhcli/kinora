"""Named load scenarios — reader behaviour mapped onto the API (kinora.md §4/§5.6).

A *scenario* binds a reader :class:`ReaderPersona` to the concrete endpoint
sequence one reading session issues, plus the journey prologue (open a session)
and how each reader action becomes a request. The load runner instantiates a
scenario per virtual user, gives it a session id + a seed, and asks it for the
request it should issue next — so scenarios are the single place the §5.6 wire
contract (``POST /sessions``, ``/intent``, ``/seek``) is encoded.

This module is pure request *planning*: it produces :class:`PlannedRequest`
descriptors (method, path, json, an endpoint label for the report, and a success
predicate). The runner executes them against a :class:`Transport`. That keeps the
scenarios trivially unit-testable — we assert the planned request stream for a
seeded reader without any I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from app.reliability.reader_model import (
    ActionKind,
    ReaderAction,
    ReaderModel,
    ReaderPersona,
)
from app.reliability.transport import Response

#: Verdict that decides whether a Response counts as success for the report.
SuccessPredicate = Callable[[Response], bool]


def _default_ok(resp: Response) -> bool:
    """Default success: any 2xx."""
    return resp.ok


def _intent_ok(resp: Response) -> bool:
    """Intent success: 2xx, or a 429 (the write rate-limit is expected backpressure)."""
    return resp.ok or resp.status == 429


@dataclass(frozen=True, slots=True)
class PlannedRequest:
    """One request the runner should issue (the unit a scenario emits).

    ``endpoint`` is the *label* the report buckets by (the route template, not
    the concrete path) so per-session ids don't explode cardinality (§12.5).
    """

    method: str
    path: str
    endpoint: str
    json: dict[str, object] | None = None
    is_ok: SuccessPredicate = _default_ok


# --------------------------------------------------------------------------- #
# The endpoint labels (stable report buckets)
# --------------------------------------------------------------------------- #

EP_CREATE_SESSION = "POST /sessions"
EP_INTENT = "POST /sessions/{id}/intent"
EP_SEEK = "POST /sessions/{id}/seek"
EP_GET_SESSION = "GET /sessions/{id}"
EP_LOGIN = "POST /auth/login"
EP_LIBRARY = "GET /books"


@dataclass
class ScenarioSession:
    """One virtual reader's live scenario state for a run.

    Holds the bound reader model + the session id the prologue created, and turns
    each :class:`ReaderAction` into a :class:`PlannedRequest`. ``IDLE`` actions
    emit *no* request (the runner honours think-time), which is exactly the §4.7
    "idle reader generates nothing" property surfaced as a gap in the stream.
    """

    scenario: Scenario
    session_id: str
    book_id: str
    seed: int
    _model: ReaderModel = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._model = ReaderModel(
            persona=self.scenario.persona,
            start_word=self.scenario.start_word,
            seed=self.seed,
        )

    def prologue(self) -> PlannedRequest:
        """The request that opens the session (issued once, before the loop)."""
        return PlannedRequest(
            method="POST",
            path="/sessions",
            endpoint=EP_CREATE_SESSION,
            json={"book_id": self.book_id, "focus_word": self.scenario.start_word},
        )

    def action_to_request(self, action: ReaderAction) -> PlannedRequest | None:
        """Map a reader action to a request (``None`` for an idle pause)."""
        base = f"/sessions/{self.session_id}"
        if action.kind is ActionKind.IDLE:
            return None
        if action.kind is ActionKind.SEEK:
            return PlannedRequest(
                method="POST",
                path=f"{base}/seek",
                endpoint=EP_SEEK,
                json={"word": int(action.seek_word or action.focus_word)},
                is_ok=_default_ok,
            )
        # INTENT (steady reading or skim).
        return PlannedRequest(
            method="POST",
            path=f"{base}/intent",
            endpoint=EP_INTENT,
            json={
                "focus_word": int(action.focus_word),
                "velocity": round(action.velocity_wps, 4),
                "mode": "viewer",
            },
            is_ok=_intent_ok,
        )

    def requests(self, *, duration_s: float) -> Iterator[PlannedRequest]:
        """Yield the request stream this reader issues over the run window."""
        for action in self._model.steps(duration_s=duration_s):
            planned = self.action_to_request(action)
            if planned is not None:
                yield planned


@dataclass(frozen=True, slots=True)
class Scenario:
    """A named realistic-reader load scenario."""

    name: str
    persona: ReaderPersona
    start_word: int = 0
    description: str = ""

    def session(self, *, session_id: str, book_id: str, seed: int) -> ScenarioSession:
        """Bind this scenario to one virtual reader."""
        return ScenarioSession(
            scenario=self, session_id=session_id, book_id=book_id, seed=seed
        )


# --------------------------------------------------------------------------- #
# The standard scenario library (mapped to §4.10 reader behaviours)
# --------------------------------------------------------------------------- #


def steady_reader() -> Scenario:
    """An engaged reader dwelling forward at ~240 wpm — the §4.10 happy path."""
    return Scenario(
        name="steady_reader",
        persona=ReaderPersona(
            name="engaged", p_skim=0.01, p_seek=0.005, p_pause=0.02
        ),
        description="Engaged forward reader; rare seeks/pauses (the §4.5 sawtooth).",
    )


def skim_storm() -> Scenario:
    """Many readers skimming fast — the §4.6 trajectory-unstable stress case."""
    return Scenario(
        name="skim_storm",
        persona=ReaderPersona(
            name="skimmer",
            p_skim=0.4,
            skim_velocity_mult=5.0,
            p_seek=0.02,
            p_pause=0.01,
        ),
        description="Heavy skim bursts; promotion suspended, rides the keyframe ladder.",
    )


def seek_thrash() -> Scenario:
    """Readers jumping around constantly — the §4.8 cancel/bridge/re-seed storm."""
    return Scenario(
        name="seek_thrash",
        persona=ReaderPersona(
            name="seeker", p_seek=0.25, p_skim=0.05, p_pause=0.02, seek_span_words=8000
        ),
        description="Frequent far seeks; stresses cancellation + instant bridge.",
    )


def cold_open() -> Scenario:
    """Readers all opening a fresh book at word 0 — the §4.10 t=0 burst."""
    return Scenario(
        name="cold_open",
        persona=ReaderPersona(name="opener", p_skim=0.0, p_seek=0.0, p_pause=0.0),
        start_word=0,
        description="Synchronized cold start; the initial committed burst to H.",
    )


def idle_dipper() -> Scenario:
    """Readers who frequently put the book down — the §4.7 idle-pause case."""
    return Scenario(
        name="idle_dipper",
        persona=ReaderPersona(
            name="dipper", p_pause=0.2, mean_pause_s=12.0, p_skim=0.02, p_seek=0.02
        ),
        description="Frequent long pauses; proves idle generates no traffic.",
    )


#: The named-scenario registry the CLI `--scenario`/`--profile` resolves against.
SCENARIOS: dict[str, Callable[[], Scenario]] = {
    "steady_reader": steady_reader,
    "skim_storm": skim_storm,
    "seek_thrash": seek_thrash,
    "cold_open": cold_open,
    "idle_dipper": idle_dipper,
}


def get_scenario(name: str) -> Scenario:
    """Resolve a scenario by name (``ValueError`` for an unknown name)."""
    factory = SCENARIOS.get(name)
    if factory is None:
        raise ValueError(
            f"unknown scenario {name!r}; known: {', '.join(sorted(SCENARIOS))}"
        )
    return factory()


__all__ = [
    "EP_CREATE_SESSION",
    "EP_GET_SESSION",
    "EP_INTENT",
    "EP_LIBRARY",
    "EP_LOGIN",
    "EP_SEEK",
    "SCENARIOS",
    "PlannedRequest",
    "Scenario",
    "ScenarioSession",
    "SuccessPredicate",
    "cold_open",
    "get_scenario",
    "idle_dipper",
    "seek_thrash",
    "skim_storm",
    "steady_reader",
]
