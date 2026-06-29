"""Own every source of entropy for the duration of a run (the FoundationDB rule:
the simulator owns randomness, time, and ids — nothing leaks from the OS).

A simulation is only replayable if *every* nondeterministic input is a function of
the seed. The framework's own subsystems already draw from a seeded
:class:`~app.verification.simulation.core.Prng`, but the **production code under
test** does not know it is in a simulation — it calls :func:`uuid.uuid4`,
:func:`random.random`, and ``time.time`` directly (e.g. ``app.db.base.new_id``
mints job ids with ``uuid.uuid4``). Those calls would otherwise inject fresh
entropy on every run and make byte-identical replay impossible.

:func:`deterministic_entropy` is a context manager that, for the span of a run,
redirects those global sources to draw from a seeded :class:`Prng`:

* ``uuid.uuid4`` → a UUID built from PRNG bytes (so ``new_id`` is stable);
* the global :mod:`random` module → reseeded (so any incidental ``random.*`` call
  is stable);
* ``time.time`` / ``time.monotonic`` → the simulation's virtual clock (so a stray
  wall-clock read sees virtual time, not real time).

It restores everything on exit, so it never leaks into the rest of the test suite.
This is exactly the "no uncontrolled nondeterminism" discipline that lets a
failing seed replay to the byte.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from app.verification.simulation.core import Prng


@contextmanager
def deterministic_entropy(
    prng: Prng,
    *,
    now_ms: Callable[[], int] | None = None,
) -> Iterator[None]:
    """Redirect global entropy (uuid/random/time) to the seed for this block.

    ``prng`` supplies deterministic uuid bytes and reseeds the global RNG;
    ``now_ms`` (if given) backs ``time.time``/``time.monotonic`` with the virtual
    clock. All originals are restored on exit — guaranteed, even on exception.
    """
    orig_uuid4 = uuid.uuid4
    orig_time = time.time
    orig_monotonic = time.monotonic
    orig_rand_state = random.getstate()

    # Derive an independent stream for id minting so reseeding random does not
    # perturb the uuid stream or vice-versa.
    id_stream = prng.split("uuid")

    def _fake_uuid4() -> uuid.UUID:
        # Build a version-4 UUID from 128 deterministic PRNG bits.
        hi = id_stream.randint(0, (1 << 64) - 1)
        lo = id_stream.randint(0, (1 << 64) - 1)
        value = (hi << 64) | lo
        # Set the version (4) and variant (RFC 4122) bits, as uuid4 would.
        value &= ~(0xF000 << 64)
        value |= 0x4000 << 64
        value &= ~(0xC000)
        value |= 0x8000
        return uuid.UUID(int=value & ((1 << 128) - 1))

    uuid.uuid4 = _fake_uuid4
    # Reseed the global RNG from a stable draw so any incidental random.* is fixed.
    random.seed(prng.split("global-random").randint(0, (1 << 63) - 1))

    if now_ms is not None:
        time.time = lambda: now_ms() / 1000.0
        time.monotonic = lambda: now_ms() / 1000.0

    try:
        yield
    finally:
        uuid.uuid4 = orig_uuid4
        time.time = orig_time
        time.monotonic = orig_monotonic
        random.setstate(orig_rand_state)


__all__ = ["deterministic_entropy"]
