"""Templated notifications with a localization hook.

A notification's wording is *not* hard-coded at the call site. Each
(``DomainEvent``, ``Channel``) pair resolves to a :class:`MessageTemplate` whose
``subject``/``body`` are ``str.format``-style strings interpolated with the
event's ``data``. Templates are grouped into per-:class:`Locale` **catalogs** so
the same notification renders in the recipient's language; an unknown locale
falls back to the default catalog (``en``), and an unknown (event, channel) pair
falls back to a generic template so a new event never crashes delivery.

This is deliberately a tiny, dependency-free engine (no Jinja): the message
shapes are short and the safety of ``{var}`` interpolation with a defaulting
mapping is easy to reason about and test. Real i18n catalogs (gettext / a vendor)
slot in behind the same :class:`TemplateRegistry` interface later.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.notifications.errors import TemplateNotFoundError
from app.notifications.events import DomainEvent
from app.notifications.models import Channel, RenderedMessage

#: The fallback locale every catalog defaults to.
DEFAULT_LOCALE = "en"


class _SafeDict(dict[str, object]):
    """A mapping that renders missing ``{keys}`` as ``{key}`` instead of raising.

    A template referencing a variable the event didn't supply should degrade
    gracefully (leave the placeholder) rather than blow up delivery.
    """

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(frozen=True, slots=True)
class MessageTemplate:
    """A subject/body pair for one (event, channel, locale)."""

    subject: str
    body: str

    def render(self, data: dict[str, object], *, locale: str) -> RenderedMessage:
        """Interpolate ``data`` into the subject/body (missing vars left as-is)."""
        safe = _SafeDict(data)
        return RenderedMessage(
            subject=self.subject.format_map(safe),
            body=self.body.format_map(safe),
            locale=locale,
        )


#: A generic last-resort template so an unmapped event still produces *something*.
_GENERIC = MessageTemplate(
    subject="Kinora update",
    body="There's an update on your Kinora reading: {event}.",
)


# --------------------------------------------------------------------------- #
# Built-in catalogs
# --------------------------------------------------------------------------- #

# English (default). Keys: (event, channel) → template. Where a channel-specific
# wording isn't needed, the (event, None) entry is the per-event default.
_EN: dict[tuple[DomainEvent, Channel | None], MessageTemplate] = {
    (DomainEvent.BOOK_READY, None): MessageTemplate(
        subject="{title} is ready to watch",
        body="Your book “{title}” finished preparing and is ready to read-watch.",
    ),
    (DomainEvent.BOOK_FAILED, None): MessageTemplate(
        subject="Couldn’t prepare {title}",
        body="We hit a problem preparing “{title}”. You can try importing it again.",
    ),
    (DomainEvent.RENDER_DONE, None): MessageTemplate(
        subject="A new scene is ready",
        body="A fresh shot just finished rendering for “{title}”.",
    ),
    (DomainEvent.REGEN_DONE, None): MessageTemplate(
        subject="Your edit is in",
        body="The shot you directed has been regenerated for “{title}”.",
    ),
    (DomainEvent.BUDGET_LOW, None): MessageTemplate(
        subject="Generation budget is running low",
        body="Only {remaining_s}s of video budget remain; playback may step down to "
        "keyframes.",
    ),
    (DomainEvent.CONFLICT_SURFACED, None): MessageTemplate(
        subject="A continuity choice needs you",
        body="The crew surfaced a continuity conflict that needs your decision in "
        "Director mode.",
    ),
    (DomainEvent.RENDER_DEADLETTER, None): MessageTemplate(
        subject="A shot couldn’t be rendered",
        body="A shot failed repeatedly and dropped to the Ken-Burns fallback for "
        "“{title}”.",
    ),
    (DomainEvent.DIGEST_READY, None): MessageTemplate(
        subject="Your Kinora digest ({count} updates)",
        body="Here’s what happened while you were away:\n{summary}",
    ),
}

# A spanish catalog to prove the localization hook end-to-end (subset; falls back).
_ES: dict[tuple[DomainEvent, Channel | None], MessageTemplate] = {
    (DomainEvent.BOOK_READY, None): MessageTemplate(
        subject="{title} ya está listo",
        body="Tu libro “{title}” terminó de prepararse y ya puedes verlo.",
    ),
    (DomainEvent.BUDGET_LOW, None): MessageTemplate(
        subject="El presupuesto de generación es bajo",
        body="Solo quedan {remaining_s}s de vídeo; la reproducción puede bajar de "
        "calidad.",
    ),
    (DomainEvent.CONFLICT_SURFACED, None): MessageTemplate(
        subject="Una decisión de continuidad te espera",
        body="El equipo encontró un conflicto de continuidad que necesita tu decisión.",
    ),
}


class TemplateRegistry:
    """Resolves (event, channel, locale) → :class:`MessageTemplate`.

    Lookup precedence within a locale: an exact (event, channel) entry, then the
    per-event (event, None) default. Across locales: the requested locale, then
    the default locale, then the built-in generic template.
    """

    def __init__(
        self,
        catalogs: dict[str, dict[tuple[DomainEvent, Channel | None], MessageTemplate]]
        | None = None,
        *,
        default_locale: str = DEFAULT_LOCALE,
    ) -> None:
        self._catalogs = catalogs if catalogs is not None else {"en": _EN, "es": _ES}
        self._default_locale = default_locale

    def register(
        self,
        locale: str,
        event: DomainEvent,
        template: MessageTemplate,
        *,
        channel: Channel | None = None,
    ) -> None:
        """Add/override a template for a locale (callers can extend the catalog)."""
        self._catalogs.setdefault(locale, {})[(event, channel)] = template

    def available_locales(self) -> list[str]:
        return sorted(self._catalogs)

    def resolve(
        self, event: DomainEvent, channel: Channel, locale: str
    ) -> MessageTemplate:
        """Find the best template for (event, channel) in ``locale`` with fallbacks."""
        for loc in self._locale_chain(locale):
            catalog = self._catalogs.get(loc)
            if catalog is None:
                continue
            template = catalog.get((event, channel)) or catalog.get((event, None))
            if template is not None:
                return template
        return _GENERIC

    def render(
        self,
        event: DomainEvent,
        channel: Channel,
        *,
        locale: str,
        data: dict[str, object],
        strict: bool = False,
    ) -> RenderedMessage:
        """Resolve + render a message. With ``strict`` a miss raises instead of generic."""
        template = self.resolve(event, channel, locale)
        if strict and template is _GENERIC and not self._has_specific(event, channel, locale):
            raise TemplateNotFoundError(
                f"no template for ({event.value}, {channel.value}, {locale})"
            )
        rendered = template.render(data, locale=locale)
        # Surface the requested locale even when we fell back to the default catalog.
        return rendered.model_copy(update={"locale": self._effective_locale(locale)})

    def _has_specific(self, event: DomainEvent, channel: Channel, locale: str) -> bool:
        for loc in self._locale_chain(locale):
            catalog = self._catalogs.get(loc, {})
            if (event, channel) in catalog or (event, None) in catalog:
                return True
        return False

    def _locale_chain(self, locale: str) -> list[str]:
        chain = [locale]
        # "en-US" → also try "en".
        if "-" in locale:
            chain.append(locale.split("-", 1)[0])
        if self._default_locale not in chain:
            chain.append(self._default_locale)
        return chain

    def _effective_locale(self, requested: str) -> str:
        for loc in self._locale_chain(requested):
            if loc in self._catalogs:
                return requested if loc == requested else loc
        return self._default_locale


__all__ = ["DEFAULT_LOCALE", "MessageTemplate", "TemplateRegistry"]
