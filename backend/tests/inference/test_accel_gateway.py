"""Gateway + adapter tests — composed cache/speculation/prefix + provider seam."""

from __future__ import annotations

from app.inference.accel.adapters import ChatBackend, EmbeddingAdapter
from app.inference.accel.clock import FakeClock
from app.inference.accel.constrained import JsonValueConstraint
from app.inference.accel.fakes import (
    CountingBackend,
    HashEmbedder,
    ScriptedDraft,
    ScriptedTarget,
    StaticBackend,
)
from app.inference.accel.gateway import AcceleratedGateway
from app.inference.accel.metrics import CacheMetrics
from app.inference.accel.prefix_reuse import KVReuseBook
from app.inference.accel.protocol import GenerationRequest
from app.inference.accel.semantic_cache import CacheConfig, SemanticCache

REQ = GenerationRequest.from_prompt("explain photosynthesis", max_tokens=20)


def _cache(**kw: object) -> SemanticCache:
    return SemanticCache(HashEmbedder(dim=16), clock=FakeClock(), metrics=CacheMetrics(), **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Cache layer
# --------------------------------------------------------------------------- #


async def test_gateway_caches_base_calls() -> None:
    base = CountingBackend(StaticBackend("plants eat light"))
    gw = AcceleratedGateway(base, cache=_cache(), clock=FakeClock())
    r1 = await gw.generate(REQ)
    r2 = await gw.generate(REQ)
    assert base.calls == 1  # second served from cache
    assert r1.text == "plants eat light"
    assert r2.meta["cache"] == "exact"


async def test_gateway_without_cache_calls_base_each_time() -> None:
    base = CountingBackend(StaticBackend("x"))
    gw = AcceleratedGateway(base, clock=FakeClock())
    await gw.generate(REQ)
    await gw.generate(REQ)
    assert base.calls == 2


# --------------------------------------------------------------------------- #
# Speculative layer
# --------------------------------------------------------------------------- #


async def test_gateway_speculation_matches_base_and_caches() -> None:
    sentence = ["sun", "powers", "leaves"]
    target = ScriptedTarget(default_fn=lambda r: sentence)
    draft = ScriptedDraft(oracle=lambda r: sentence)
    # base is the same oracle so we can assert equivalence
    base = StaticBackend("sun powers leaves")
    gw = AcceleratedGateway(
        base, cache=_cache(), draft=draft, target=target, clock=FakeClock()
    )
    out = await gw.generate(REQ)
    assert out.tokens == tuple(sentence)
    # cached: second call hits and does not re-run speculation
    target.verify_calls = 0
    out2 = await gw.generate(REQ)
    assert out2.meta["cache"] == "exact"
    assert target.verify_calls == 0
    assert gw.speculative is not None


# --------------------------------------------------------------------------- #
# Prefix-reuse bookkeeping layer
# --------------------------------------------------------------------------- #


async def test_gateway_records_prefix_reuse() -> None:
    book = KVReuseBook(block_size=2)
    base = StaticBackend("answer here now")
    gw = AcceleratedGateway(base, prefix_book=book, clock=FakeClock())
    # First call: registers the prompt; second identical prompt would reuse,
    # but since the gateway has no cache here it recomputes and the book records
    # both requests.
    await gw.generate(GenerationRequest.from_prompt("alpha beta gamma delta"))
    await gw.generate(GenerationRequest.from_prompt("alpha beta gamma delta epsilon"))
    snap = book.metrics.snapshot()
    assert snap.requests == 2
    # Second prompt shares the 4-token prefix -> some tokens reused.
    assert snap.prompt_tokens_reused > 0
    assert gw.prefix_book is book


# --------------------------------------------------------------------------- #
# Orthogonal entry points
# --------------------------------------------------------------------------- #


async def test_gateway_race() -> None:
    base = StaticBackend("base")
    gw = AcceleratedGateway(base, clock=FakeClock())
    from app.inference.accel.fanout import ProviderCandidate

    out = await gw.race(
        REQ,
        [ProviderCandidate("a", StaticBackend("from a").generate)],
    )
    assert out.winner == "a"


async def test_gateway_generate_constrained_uses_cache() -> None:
    base = CountingBackend(StaticBackend('{"k":1}'))
    gw = AcceleratedGateway(base, cache=_cache(), clock=FakeClock())
    out = await gw.generate_constrained(REQ, JsonValueConstraint("object"))
    assert out.value == {"k": 1}
    # The cached result is reused on a second constrained call.
    out2 = await gw.generate_constrained(REQ, JsonValueConstraint("object"))
    assert out2.value == {"k": 1}
    assert base.calls == 1


# --------------------------------------------------------------------------- #
# Adapters (provider seam)
# --------------------------------------------------------------------------- #


class _FakeChat:
    def __init__(self) -> None:
        self.seen: list[tuple[list[dict[str, str]], str]] = []

    async def chat(self, messages, model, **kwargs):  # type: ignore[no-untyped-def]
        self.seen.append((messages, model))

        class _R:
            text = "adapter says hi"
            model = "qwen-x"
            finish_reason = "stop"
            input_tokens = 7
            output_tokens = 3

        return _R()


async def test_chat_backend_adapter() -> None:
    chat = _FakeChat()
    backend = ChatBackend(chat, model="qwen-x")
    out = await backend.generate(
        GenerationRequest.from_messages(
            [{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=50
        )
    )
    assert out.text == "adapter says hi"
    assert out.tokens == ("adapter", "says", "hi")
    assert out.input_tokens == 7
    assert out.output_tokens == 3
    # Messages were converted to provider shape and model overridden.
    msgs, model = chat.seen[0]
    assert model == "qwen-x"
    assert msgs == [{"role": "user", "content": "hi"}]


class _FakeEmbed:
    async def embed_texts(self, texts):  # type: ignore[no-untyped-def]
        return [[0.0, 1.0, 0.0] for _ in texts]


async def test_embedding_adapter() -> None:
    emb = EmbeddingAdapter(_FakeEmbed())
    v = await emb.embed("anything")
    assert v == (0.0, 1.0, 0.0)


async def test_chat_backend_into_gateway_end_to_end() -> None:
    chat = _FakeChat()
    backend = ChatBackend(chat)
    gw = AcceleratedGateway(
        backend,
        cache=SemanticCache(
            EmbeddingAdapter(_FakeEmbed()), clock=FakeClock(), config=CacheConfig()
        ),
        clock=FakeClock(),
    )
    r1 = await gw.generate(REQ)
    r2 = await gw.generate(REQ)
    assert r1.text == "adapter says hi"
    assert r2.meta["cache"] == "exact"
    assert len(chat.seen) == 1  # cached
