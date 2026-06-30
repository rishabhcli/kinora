"""Descriptor format: loading (YAML/JSON/path/dict), validation, capability mapping."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.providers.types import WanMode, WanSpec
from app.video.adapters.open.descriptor import (
    ProviderDescriptor,
    _decode_inline_b64,
    load_descriptor,
)
from app.video.adapters.open.registry import bundled_descriptor_paths, load_bundled


def test_load_bundled_yaml_descriptors_parse() -> None:
    for stem in ("comfyui_example", "openapi_example", "fictional_nebula"):
        desc = load_bundled(stem)
        assert isinstance(desc, ProviderDescriptor)
        assert desc.name and desc.model


def test_load_bundled_json_descriptor_parses() -> None:
    desc = load_bundled("fictional_pulsar")
    assert desc.name == "pulsar-1"
    assert WanMode.TEXT_TO_VIDEO in desc.to_capabilities().modes


def test_bundled_paths_nonempty_and_sorted() -> None:
    paths = bundled_descriptor_paths()
    stems = [p.stem for p in paths]
    assert {"comfyui_example", "openapi_example", "fictional_nebula", "fictional_pulsar"} <= set(
        stems
    )
    assert stems == sorted(stems)


def test_load_from_dict() -> None:
    raw = {
        "name": "inline-model",
        "model": "inline-v1",
        "transport": {"base_url": "https://x.test", "auth_scheme": "bearer"},
        "submit": {"path": "go", "body_template": {"prompt": "{{prompt}}"}},
        "poll": {"path": "go/{{task_id}}"},
    }
    desc = load_descriptor(raw)
    assert desc.name == "inline-model"
    assert desc.submit.path == "go"


def test_load_from_json_string() -> None:
    raw = {
        "name": "json-str",
        "model": "v",
        "transport": {"base_url": "https://x.test"},
        "submit": {"path": "go"},
        "poll": {"path": "go/{{task_id}}"},
    }
    desc = load_descriptor(json.dumps(raw))
    assert desc.name == "json-str"


def test_load_yaml_string() -> None:
    yaml_text = (
        "name: yaml-str\n"
        "model: v\n"
        "transport:\n  base_url: https://x.test\n"
        "submit:\n  path: go\n"
        "poll:\n  path: go/{{task_id}}\n"
    )
    desc = load_descriptor(yaml_text)
    assert desc.name == "yaml-str"
    assert desc.transport.base_url == "https://x.test"


def test_missing_required_field_raises() -> None:
    with pytest.raises(ValidationError):
        load_descriptor({"name": "x"})  # missing model/transport/submit/poll


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        load_descriptor(
            {
                "name": "x",
                "model": "v",
                "transport": {"base_url": "u"},
                "submit": {"path": "p"},
                "poll": {"path": "p2"},
                "surprise": 1,
            }
        )


def test_bad_auth_scheme_rejected() -> None:
    with pytest.raises(ValidationError):
        load_descriptor(
            {
                "name": "x",
                "model": "v",
                "transport": {"base_url": "u", "auth_scheme": "hmac"},
                "submit": {"path": "p"},
                "poll": {"path": "p2"},
            }
        )


def test_capabilities_round_trip() -> None:
    desc = load_bundled("fictional_nebula")
    caps = desc.to_capabilities()
    assert caps.name == "nebula-dream-3"
    assert caps.max_reference_images == 4
    assert caps.supports_audio is True
    # the spec the descriptor advertises must pass its own capability check
    spec = WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="x", duration_s=5, resolution="720P")
    assert caps.supports(spec)


def test_decode_inline_b64_variants() -> None:
    import base64

    raw = b"clipbytes"
    b64 = base64.b64encode(raw).decode()
    assert _decode_inline_b64(b64) == raw
    assert _decode_inline_b64(f"data:video/mp4;base64,{b64}") == raw
    assert _decode_inline_b64(None) is None
    assert _decode_inline_b64("") is None
    assert _decode_inline_b64(123) is None  # non-string input → None
