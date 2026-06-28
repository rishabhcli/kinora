"""Unit tests for the glossary / do-not-translate layer."""

from __future__ import annotations

import pytest

from app.translation.errors import GlossaryError
from app.translation.glossary import Glossary, GlossaryEntry, from_character_names


def test_dnt_term_protected_and_restored_verbatim() -> None:
    g = Glossary([GlossaryEntry(source="Elsa", do_not_translate=True)])
    masked, restorations = g.protect("Elsa walked in", target_lang="fr")
    assert "Elsa" not in masked  # masked out
    assert restorations == ["Elsa"]
    restored = Glossary.restore(masked, restorations)
    assert "Elsa" in restored


def test_forced_target_translation() -> None:
    g = Glossary([GlossaryEntry(source="Snow Queen", targets={"fr": "Reine des neiges"})])
    masked, restorations = g.protect("the Snow Queen ruled", target_lang="fr")
    assert restorations == ["Reine des neiges"]
    assert "Reine des neiges" in Glossary.restore(masked, restorations)


def test_longest_match_wins() -> None:
    g = Glossary(
        [
            GlossaryEntry(source="Snow Queen", do_not_translate=True),
            GlossaryEntry(source="Queen", do_not_translate=True),
        ]
    )
    hits = g.find("the Snow Queen and the Queen")
    # "Snow Queen" claims its span; the standalone "Queen" still matches once.
    matched = sorted(h.matched_text for h in hits)
    assert "Snow Queen" in matched
    assert matched.count("Queen") == 1


def test_whole_word_matching() -> None:
    g = Glossary([GlossaryEntry(source="art", do_not_translate=True)])
    # "art" inside "Bart" must NOT match (word boundary).
    assert g.find("Bart smiled") == []
    assert len(g.find("the art of war")) == 1


def test_case_insensitive_default() -> None:
    g = Glossary([GlossaryEntry(source="elsa", do_not_translate=True)])
    assert len(g.find("ELSA and Elsa")) == 2


def test_verify_flags_missing_forced_term() -> None:
    g = Glossary([GlossaryEntry(source="Snow Queen", targets={"fr": "Reine des neiges"})])
    warnings = g.verify("the Snow Queen", "la sorcière", target_lang="fr")
    assert any("missing" in w for w in warnings)
    ok = g.verify("the Snow Queen", "la Reine des neiges arrive", target_lang="fr")
    assert ok == []


def test_invalid_entry_raises() -> None:
    with pytest.raises(GlossaryError):
        Glossary([GlossaryEntry(source="", do_not_translate=True)])
    with pytest.raises(GlossaryError):
        Glossary([GlossaryEntry(source="x")])  # neither DNT nor targets


def test_bump_version_invalidates() -> None:
    g = Glossary([GlossaryEntry(source="Elsa", do_not_translate=True)])
    v0 = g.version
    assert g.bump_version() == v0 + 1


def test_from_character_names_builds_dnt() -> None:
    g = from_character_names({"char_elsa": "Elsa", "char_anna": "Anna", "char_x": None})
    sources = sorted(e.source for e in g.entries)
    assert sources == ["Anna", "Elsa"]
    assert all(e.do_not_translate for e in g.entries)


def test_add_is_upsert() -> None:
    g = Glossary([GlossaryEntry(source="Elsa", do_not_translate=True)])
    g.add(GlossaryEntry(source="Elsa", targets={"fr": "Elsa"}))
    # Still a single entry for that source.
    assert len([e for e in g.entries if e.source == "Elsa"]) == 1


def test_target_for_region_fallback() -> None:
    entry = GlossaryEntry(source="x", targets={"pt": "xx"})
    assert entry.target_for("pt-BR") == "xx"  # falls back to primary
