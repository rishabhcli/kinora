"""Unit tests for literary-device detection → visual intent (no network).

Simile, copular metaphor, personification, symbolism — each must produce a
concrete ``visual_intent`` without inventing canon entities (§10).
"""

from __future__ import annotations

from app.agents.comprehension.devices import detect_devices, visual_intent_summary


def test_simile_detected() -> None:
    devs = detect_devices("Her voice was soft like a falling snowflake.")
    kinds = {d.kind for d in devs}
    assert "simile" in kinds
    sim = next(d for d in devs if d.kind == "simile")
    assert "snowflake" in sim.vehicle.lower()
    assert sim.visual_intent  # a non-empty staging instruction


def test_copular_metaphor_detected() -> None:
    devs = detect_devices("His heart was a fortress of cold stone.")
    metas = [d for d in devs if d.kind == "metaphor"]
    assert metas
    assert "fortress" in metas[0].vehicle.lower()
    assert metas[0].visual_intent


def test_personification_detected() -> None:
    devs = detect_devices("The wind whispered through the broken windows.")
    pers = [d for d in devs if d.kind == "personification"]
    assert pers
    assert pers[0].tenor == "wind"
    assert "wind" in pers[0].visual_intent.lower()


def test_symbol_detected() -> None:
    devs = detect_devices("A single raven watched from the bare branch.")
    syms = [d for d in devs if d.kind == "symbol"]
    assert any(d.tenor == "raven" for d in syms)


def test_no_device_in_plain_text() -> None:
    devs = detect_devices("He opened the gate and stepped onto the gravel path.")
    # Plain action prose should not hallucinate figures.
    assert all(d.kind != "metaphor" for d in devs)


def test_max_devices_cap_and_dedup() -> None:
    text = "The wind whispered. The wind whispered again. A raven watched. A mirror gleamed."
    devs = detect_devices(text, max_devices=2)
    assert len(devs) <= 2


def test_visual_intent_summary_concatenates() -> None:
    devs = detect_devices("Her grief was a stone, and the river of time flowed on.")
    summary = visual_intent_summary(devs)
    assert ";" in summary or summary  # joined intents
    assert isinstance(summary, str)
