"""Deterministic tests for app.sechardening.*

No infra, no network, no live keys.  All behaviour is pure / offline.

Coverage:
* keys       — traversal rejection, bad-charset rejection, canonicalization
* uploads    — magic-byte match + mismatch, size enforcement
* domains    — allow-list allow/deny incl. scheme + suffix matching
* redaction  — secrets masked in nested log events; Secret repr + serialization
"""

from __future__ import annotations

import json
import os

import pytest

# Ensure the test suite can boot Settings without a real key.
os.environ.setdefault("DASHSCOPE_API_KEY", "test")
os.environ.setdefault("APP_ENV", "local")

# ---------------------------------------------------------------------------
# app.sechardening.keys
# ---------------------------------------------------------------------------


class TestNormalizeKey:
    """Tests for :func:`app.sechardening.keys.normalize_key`."""

    from app.sechardening.keys import KeyValidationError, normalize_key

    def test_simple_filename_passthrough(self) -> None:
        from app.sechardening.keys import normalize_key

        assert normalize_key("book.pdf") == "book.pdf"

    def test_hierarchical_key(self) -> None:
        from app.sechardening.keys import normalize_key

        assert normalize_key("books/123/cover.jpg") == "books/123/cover.jpg"

    def test_prefix_prepended(self) -> None:
        from app.sechardening.keys import normalize_key

        assert normalize_key("cover.jpg", prefix="media/") == "media/cover.jpg"

    def test_collapse_double_slashes(self) -> None:
        from app.sechardening.keys import normalize_key

        assert normalize_key("books//123//page.png") == "books/123/page.png"

    def test_strip_leading_slash(self) -> None:
        from app.sechardening.keys import KeyValidationError, normalize_key

        with pytest.raises(KeyValidationError) as exc_info:
            normalize_key("/etc/passwd")
        assert exc_info.value.reason == "absolute_path"

    def test_dotdot_traversal_rejected(self) -> None:
        from app.sechardening.keys import KeyValidationError, normalize_key

        with pytest.raises(KeyValidationError) as exc_info:
            normalize_key("books/../../../etc/passwd")
        assert exc_info.value.reason == "traversal"

    def test_single_dot_traversal_rejected(self) -> None:
        from app.sechardening.keys import KeyValidationError, normalize_key

        with pytest.raises(KeyValidationError) as exc_info:
            normalize_key("./relative")
        assert exc_info.value.reason == "traversal"

    def test_url_encoded_dotdot_rejected(self) -> None:
        from app.sechardening.keys import KeyValidationError, normalize_key

        with pytest.raises(KeyValidationError) as exc_info:
            normalize_key("books/%2e%2e/secret.pdf")
        assert exc_info.value.reason == "traversal"

    def test_url_encoded_upper_dotdot_rejected(self) -> None:
        from app.sechardening.keys import KeyValidationError, normalize_key

        with pytest.raises(KeyValidationError) as exc_info:
            normalize_key("books/%2E%2E/secret.pdf")
        assert exc_info.value.reason == "traversal"

    def test_nul_byte_rejected(self) -> None:
        from app.sechardening.keys import KeyValidationError, normalize_key

        with pytest.raises(KeyValidationError) as exc_info:
            normalize_key("book\x00.pdf")
        assert exc_info.value.reason == "control_chars"

    def test_control_char_rejected(self) -> None:
        from app.sechardening.keys import KeyValidationError, normalize_key

        with pytest.raises(KeyValidationError) as exc_info:
            normalize_key("book\x1f.pdf")
        assert exc_info.value.reason == "control_chars"

    def test_non_ascii_rejected(self) -> None:
        from app.sechardening.keys import KeyValidationError, normalize_key

        # Unicode outside the safe charset must be rejected.
        with pytest.raises(KeyValidationError) as exc_info:
            normalize_key("bücher/roman.pdf")
        assert exc_info.value.reason in {"unsafe_segment", "unsafe_charset"}

    def test_spaces_rejected(self) -> None:
        from app.sechardening.keys import KeyValidationError, normalize_key

        with pytest.raises(KeyValidationError) as exc_info:
            normalize_key("my book.pdf")
        assert exc_info.value.reason in {"unsafe_segment", "unsafe_charset"}

    def test_empty_after_strip_rejected(self) -> None:
        from app.sechardening.keys import KeyValidationError, normalize_key

        with pytest.raises(KeyValidationError) as exc_info:
            normalize_key("   ")
        assert exc_info.value.reason == "empty"

    def test_too_long_raw_rejected(self) -> None:
        from app.sechardening.keys import KeyValidationError, normalize_key

        with pytest.raises(KeyValidationError) as exc_info:
            normalize_key("a" * 3_000)
        assert exc_info.value.reason == "too_long"

    def test_valid_complex_key(self) -> None:
        from app.sechardening.keys import normalize_key

        result = normalize_key("books/a1B2-c3_d4/frames/00001.jpg")
        assert result == "books/a1B2-c3_d4/frames/00001.jpg"

    def test_nfc_normalization(self) -> None:
        from app.sechardening.keys import normalize_key

        # NFC vs NFD for an ASCII-only string is a no-op; both should normalize
        # to the same result.  For letters like "é" (NFD) vs "\xe9" (NFC):
        # both are non-ASCII so rejected.  Verify pure-ASCII NFC survives.
        assert normalize_key("cover.png") == "cover.png"


class TestIsSafeKey:
    def test_safe_returns_true(self) -> None:
        from app.sechardening.keys import is_safe_key

        assert is_safe_key("books/123/page.png") is True

    def test_traversal_returns_false(self) -> None:
        from app.sechardening.keys import is_safe_key

        assert is_safe_key("../secret") is False

    def test_absolute_returns_false(self) -> None:
        from app.sechardening.keys import is_safe_key

        assert is_safe_key("/etc/shadow") is False


# ---------------------------------------------------------------------------
# app.sechardening.uploads
# ---------------------------------------------------------------------------

# Minimal synthetic file headers — just enough magic bytes for each sniffer.
_PDF_HEADER = b"%PDF-1.7 some pdf content here"
_PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
_JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 20
_WEBP_HEADER = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20
_MP4_HEADER = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 20
_RANDOM_DATA = b"\x00\x01\x02\x03" * 10


class TestSniffMimeType:
    def test_pdf(self) -> None:
        from app.sechardening.uploads import sniff_mime_type

        assert sniff_mime_type(_PDF_HEADER) == "application/pdf"

    def test_png(self) -> None:
        from app.sechardening.uploads import sniff_mime_type

        assert sniff_mime_type(_PNG_HEADER) == "image/png"

    def test_jpeg(self) -> None:
        from app.sechardening.uploads import sniff_mime_type

        assert sniff_mime_type(_JPEG_HEADER) == "image/jpeg"

    def test_webp(self) -> None:
        from app.sechardening.uploads import sniff_mime_type

        assert sniff_mime_type(_WEBP_HEADER) == "image/webp"

    def test_mp4(self) -> None:
        from app.sechardening.uploads import sniff_mime_type

        assert sniff_mime_type(_MP4_HEADER) == "video/mp4"

    def test_unknown_returns_none(self) -> None:
        from app.sechardening.uploads import sniff_mime_type

        assert sniff_mime_type(_RANDOM_DATA) is None

    def test_too_short_returns_none(self) -> None:
        from app.sechardening.uploads import sniff_mime_type

        assert sniff_mime_type(b"\xff\xd8") is None


class TestValidateUpload:
    def test_pdf_valid(self) -> None:
        from app.sechardening.uploads import validate_upload

        result = validate_upload(_PDF_HEADER, "application/pdf")
        assert result == "application/pdf"

    def test_jpeg_valid(self) -> None:
        from app.sechardening.uploads import validate_upload

        result = validate_upload(_JPEG_HEADER, "image/jpeg")
        assert result == "image/jpeg"

    def test_png_valid(self) -> None:
        from app.sechardening.uploads import validate_upload

        result = validate_upload(_PNG_HEADER, "image/png")
        assert result == "image/png"

    def test_webp_valid(self) -> None:
        from app.sechardening.uploads import validate_upload

        result = validate_upload(_WEBP_HEADER, "image/webp")
        assert result == "image/webp"

    def test_mp4_valid(self) -> None:
        from app.sechardening.uploads import validate_upload

        result = validate_upload(_MP4_HEADER, "video/mp4")
        assert result == "video/mp4"

    def test_content_type_parameters_stripped(self) -> None:
        from app.sechardening.uploads import validate_upload

        # The ; charset=... suffix must not confuse the validator.
        result = validate_upload(_PDF_HEADER, "application/pdf; version=1.7")
        assert result == "application/pdf"

    def test_mismatch_declared_jpeg_actual_pdf(self) -> None:
        from app.sechardening.uploads import ContentTypeMismatchError, validate_upload

        with pytest.raises(ContentTypeMismatchError) as exc_info:
            validate_upload(_PDF_HEADER, "image/jpeg")
        err = exc_info.value
        assert err.declared == "image/jpeg"
        assert err.detected == "application/pdf"

    def test_mismatch_declared_mp4_actual_png(self) -> None:
        from app.sechardening.uploads import ContentTypeMismatchError, validate_upload

        with pytest.raises(ContentTypeMismatchError) as exc_info:
            validate_upload(_PNG_HEADER, "video/mp4")
        err = exc_info.value
        assert err.declared == "video/mp4"
        assert err.detected == "image/png"

    def test_unsupported_mime_type(self) -> None:
        from app.sechardening.uploads import ContentTypeMismatchError, validate_upload

        with pytest.raises(ContentTypeMismatchError) as exc_info:
            validate_upload(_RANDOM_DATA, "application/zip")
        assert exc_info.value.declared == "application/zip"

    def test_random_data_declared_pdf(self) -> None:
        from app.sechardening.uploads import ContentTypeMismatchError, validate_upload

        with pytest.raises(ContentTypeMismatchError) as exc_info:
            validate_upload(_RANDOM_DATA, "application/pdf")
        assert exc_info.value.detected is None  # unknown magic

    def test_file_too_large(self) -> None:
        from app.sechardening.uploads import FileTooLargeError, validate_upload

        big = b"A" * 200
        with pytest.raises(FileTooLargeError) as exc_info:
            validate_upload(big, "application/pdf", max_bytes=100)
        assert exc_info.value.size == 200
        assert exc_info.value.limit == 100

    def test_exactly_at_limit_allowed(self) -> None:
        from app.sechardening.uploads import validate_upload

        data = _PDF_HEADER + b"\x00" * (50 - len(_PDF_HEADER))
        result = validate_upload(data, "application/pdf", max_bytes=50)
        assert result == "application/pdf"

    def test_one_over_limit_rejected(self) -> None:
        from app.sechardening.uploads import FileTooLargeError, validate_upload

        data = _PDF_HEADER + b"\x00" * (51 - len(_PDF_HEADER))
        with pytest.raises(FileTooLargeError):
            validate_upload(data, "application/pdf", max_bytes=50)


# ---------------------------------------------------------------------------
# app.sechardening.domains
# ---------------------------------------------------------------------------

_ALLOWED = (
    "dashscope.aliyuncs.com",
    "minimax.io",
)


class TestIsDownloadAllowed:
    def test_exact_domain_https(self) -> None:
        from app.sechardening.domains import is_download_allowed

        assert is_download_allowed(
            "https://dashscope.aliyuncs.com/media/foo.mp4", _ALLOWED
        )

    def test_subdomain_allowed(self) -> None:
        from app.sechardening.domains import is_download_allowed

        assert is_download_allowed(
            "https://cdn.dashscope.aliyuncs.com/video.mp4", _ALLOWED
        )

    def test_deep_subdomain_allowed(self) -> None:
        from app.sechardening.domains import is_download_allowed

        assert is_download_allowed(
            "https://a.b.minimax.io/clip.mp4", _ALLOWED
        )

    def test_http_rejected(self) -> None:
        from app.sechardening.domains import is_download_allowed

        assert not is_download_allowed(
            "http://dashscope.aliyuncs.com/media/foo.mp4", _ALLOWED
        )

    def test_ftp_rejected(self) -> None:
        from app.sechardening.domains import is_download_allowed

        assert not is_download_allowed(
            "ftp://dashscope.aliyuncs.com/media/foo.mp4", _ALLOWED
        )

    def test_domain_not_on_list(self) -> None:
        from app.sechardening.domains import is_download_allowed

        assert not is_download_allowed(
            "https://evil.example.com/payload.mp4", _ALLOWED
        )

    def test_similar_domain_not_allowed(self) -> None:
        # "notdashscope.aliyuncs.com" is NOT a suffix of "dashscope.aliyuncs.com"
        from app.sechardening.domains import is_download_allowed

        assert not is_download_allowed(
            "https://notdashscope.aliyuncs.com/file.mp4", _ALLOWED
        )

    def test_prefix_spoofing_rejected(self) -> None:
        # "dashscope.aliyuncs.com.evil.com" must not match
        from app.sechardening.domains import is_download_allowed

        assert not is_download_allowed(
            "https://dashscope.aliyuncs.com.evil.com/video.mp4", _ALLOWED
        )

    def test_empty_host_rejected(self) -> None:
        from app.sechardening.domains import is_download_allowed

        assert not is_download_allowed("https:///path", _ALLOWED)

    def test_garbage_url_rejected(self) -> None:
        from app.sechardening.domains import is_download_allowed

        assert not is_download_allowed("not-a-url", _ALLOWED)


class TestAssertDownloadAllowed:
    def test_allowed_does_not_raise(self) -> None:
        from app.sechardening.domains import assert_download_allowed

        # Should not raise.
        assert_download_allowed("https://dashscope.aliyuncs.com/file.mp4", _ALLOWED)

    def test_http_raises_scheme(self) -> None:
        from app.sechardening.domains import DomainNotAllowedError, assert_download_allowed

        with pytest.raises(DomainNotAllowedError) as exc_info:
            assert_download_allowed("http://dashscope.aliyuncs.com/f.mp4", _ALLOWED)
        assert exc_info.value.reason == "scheme"

    def test_not_on_list_raises_host(self) -> None:
        from app.sechardening.domains import DomainNotAllowedError, assert_download_allowed

        with pytest.raises(DomainNotAllowedError) as exc_info:
            assert_download_allowed("https://evil.com/payload.mp4", _ALLOWED)
        assert exc_info.value.reason == "host"
        assert exc_info.value.host == "evil.com"

    def test_subdomain_allowed(self) -> None:
        from app.sechardening.domains import assert_download_allowed

        # Should not raise.
        assert_download_allowed("https://cdn.minimax.io/clip.mp4", _ALLOWED)


# ---------------------------------------------------------------------------
# app.sechardening.redaction
# ---------------------------------------------------------------------------


class TestIsSensitiveKey:
    def test_api_key_variants(self) -> None:
        from app.sechardening.redaction import is_sensitive_key

        for k in ("api_key", "ApiKey", "APIKEY", "dashscope_api_key", "openai_api_key"):
            assert is_sensitive_key(k), f"Expected sensitive: {k!r}"

    def test_password_variants(self) -> None:
        from app.sechardening.redaction import is_sensitive_key

        for k in ("password", "PASSWORD", "passwd", "user_password"):
            assert is_sensitive_key(k), f"Expected sensitive: {k!r}"

    def test_token_exact(self) -> None:
        from app.sechardening.redaction import is_sensitive_key

        assert is_sensitive_key("token")

    def test_cancel_token_not_sensitive(self) -> None:
        from app.sechardening.redaction import is_sensitive_key

        # "cancel_token" contains "token" as a substring but not as exact match
        # and does not contain any of the _SENSITIVE_SUBSTRINGS.
        # Implementation: "cancel_token" does NOT match _SENSITIVE_EXACT (not exact "token")
        # and "token" IS in _SENSITIVE_SUBSTRINGS → actually it's not, only exact match.
        # Let's check exact logic: the substring list does NOT include bare "token",
        # only "access_token" and "refresh_token".
        # "cancel_token" lowered = "cancel_token"; exact set {"token","auth","key"} → no.
        # substrings: "access_token" in "cancel_token"? no. "refresh_token"? no.
        # So cancel_token is NOT sensitive. ✓
        assert not is_sensitive_key("cancel_token")

    def test_authorization_sensitive(self) -> None:
        from app.sechardening.redaction import is_sensitive_key

        assert is_sensitive_key("Authorization")

    def test_safe_key_not_sensitive(self) -> None:
        from app.sechardening.redaction import is_sensitive_key

        for k in ("user_id", "book_id", "page_count", "event_type", "timestamp"):
            assert not is_sensitive_key(k), f"Expected NOT sensitive: {k!r}"


class TestRedactLogEvent:
    """Tests for the structlog processor :func:`app.sechardening.redaction.redact_log_event`."""

    def _run_processor(
        self,
        event_dict: dict,
        extra_keys: tuple[str, ...] = (),
    ) -> dict:
        from app.sechardening.redaction import redact_log_event

        return redact_log_event(None, "test", event_dict, extra_sensitive_keys=extra_keys)  # type: ignore[arg-type]

    def test_api_key_masked(self) -> None:
        result = self._run_processor({"api_key": "sk-supersecret", "event": "test"})
        assert result["api_key"] == "[REDACTED]"
        assert result["event"] == "test"

    def test_password_masked(self) -> None:
        result = self._run_processor({"password": "hunter2", "user": "alice"})
        assert result["password"] == "[REDACTED]"
        assert result["user"] == "alice"

    def test_nested_dict_secret_masked(self) -> None:
        # "credentials" itself matches is_sensitive_key (contains "credentials"),
        # so the ENTIRE value is replaced by [REDACTED] — including any nested dict.
        # This is the correct stricter behavior: sensitive key → whole value masked.
        result = self._run_processor(
            {
                "event": "login",
                "credentials": {"api_key": "realkey123", "user_id": "u42"},
            }
        )
        assert result["credentials"] == "[REDACTED]"
        assert result["event"] == "login"

    def test_nested_dict_non_sensitive_parent_descends(self) -> None:
        # When the parent key is NOT sensitive, the processor descends into the
        # nested dict and masks only the sensitive child keys.
        result = self._run_processor(
            {
                "event": "login",
                "context": {"api_key": "realkey123", "user_id": "u42"},
            }
        )
        assert result["context"]["api_key"] == "[REDACTED]"
        assert result["context"]["user_id"] == "u42"

    def test_list_with_nested_secrets(self) -> None:
        result = self._run_processor(
            {
                "event": "batch",
                "items": [
                    {"api_key": "k1", "name": "a"},
                    {"api_key": "k2", "name": "b"},
                ],
            }
        )
        for item in result["items"]:
            assert item["api_key"] == "[REDACTED]"
            assert item["name"] in {"a", "b"}

    def test_bearer_token_in_message_masked(self) -> None:
        result = self._run_processor(
            {"event": "calling API with Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"}
        )
        assert "Bearer [REDACTED]" in result["event"]
        assert "eyJ" not in result["event"]

    def test_sk_key_in_message_masked(self) -> None:
        result = self._run_processor({"event": "using key sk-abcdef1234567890"})
        assert "sk-abcdef" not in result["event"]
        assert "[REDACTED]" in result["event"]

    def test_extra_sensitive_keys(self) -> None:
        result = self._run_processor(
            {"event": "ok", "my_custom_field": "topsecret"},
            extra_keys=("my_custom_field",),
        )
        assert result["my_custom_field"] == "[REDACTED]"

    def test_non_sensitive_keys_pass_through(self) -> None:
        result = self._run_processor(
            {"event": "processing", "book_id": "abc-123", "page": 5}
        )
        assert result["book_id"] == "abc-123"
        assert result["page"] == 5


class TestSecret:
    """Tests for :class:`app.sechardening.redaction.Secret`."""

    def test_repr_masked(self) -> None:
        from app.sechardening.redaction import Secret

        s = Secret("sk-supersecret")
        assert repr(s) == "[REDACTED]"

    def test_str_masked(self) -> None:
        from app.sechardening.redaction import Secret

        s = Secret("hunter2")
        assert str(s) == "[REDACTED]"

    def test_format_masked(self) -> None:
        from app.sechardening.redaction import Secret

        s = Secret("hunter2")
        assert f"{s}" == "[REDACTED]"

    def test_get_secret_value(self) -> None:
        from app.sechardening.redaction import Secret

        s = Secret("real-value")
        assert s.get_secret_value() == "real-value"

    def test_equality_on_underlying_value(self) -> None:
        from app.sechardening.redaction import Secret

        a = Secret("abc")
        b = Secret("abc")
        c = Secret("xyz")
        assert a == b
        assert a != c

    def test_immutable(self) -> None:
        from app.sechardening.redaction import Secret

        s = Secret("value")
        with pytest.raises(AttributeError):
            s._value = "other"  # type: ignore[misc]

    def test_json_serialization_refused(self) -> None:
        from app.sechardening.redaction import Secret, SecretSerializationError, safe_json_dumps

        s = Secret("sk-secret")
        with pytest.raises((SecretSerializationError, TypeError)):
            safe_json_dumps({"key": s})

    def test_plain_json_dumps_refused(self) -> None:
        from app.sechardening.redaction import Secret

        s = Secret("sk-secret")
        with pytest.raises(TypeError):
            json.dumps({"key": s})

    def test_secret_masked_in_log_processor(self) -> None:
        from app.sechardening.redaction import Secret, redact_log_event

        s = Secret("sk-real-key")
        result = redact_log_event(None, "test", {"event": "ok", "data": s})  # type: ignore[arg-type]
        assert result["data"] == "[REDACTED]"

    def test_requires_str_input(self) -> None:
        from app.sechardening.redaction import Secret

        with pytest.raises(TypeError):
            Secret(12345)  # type: ignore[arg-type]


class TestSafeJsonDumps:
    def test_safe_data_serializes(self) -> None:
        from app.sechardening.redaction import safe_json_dumps

        result = safe_json_dumps({"a": 1, "b": "hello"})
        assert json.loads(result) == {"a": 1, "b": "hello"}

    def test_secret_in_list_refused(self) -> None:
        from app.sechardening.redaction import Secret, SecretSerializationError, safe_json_dumps

        with pytest.raises((SecretSerializationError, TypeError)):
            safe_json_dumps([Secret("x")])


# ---------------------------------------------------------------------------
# Config integration: sechardening settings
# ---------------------------------------------------------------------------


class TestSecHardeningConfig:
    def test_defaults_present(self) -> None:
        from app.core.config import get_settings

        s = get_settings()
        # 100 MiB default
        assert s.sechardening_upload_max_bytes == 100 * 1_024 * 1_024
        assert s.sechardening_key_max_raw_chars == 2_048

    def test_allowed_domains_default_nonempty(self) -> None:
        from app.core.config import get_settings

        s = get_settings()
        domains = s.sechardening_allowed_domains_list
        assert len(domains) > 0
        assert all(isinstance(d, str) for d in domains)

    def test_extra_redact_keys_empty_tuple(self) -> None:
        from app.core.config import get_settings

        s = get_settings()
        assert s.sechardening_extra_redact_keys_tuple == ()
