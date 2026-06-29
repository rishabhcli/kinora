"""SVID + trust-bundle (de)serialization for crossing process boundaries.

The in-process types (`X509Svid`, `TrustBundle`) are great inside one service,
but the moment a credential has to move — written to a tmpfs the way SPIFFE's
Workload API hands an SVID to a sidecar, or shipped to a peer for pinning — it
needs a stable wire form. This module is that codec, in two well-trodden formats:

* **PEM** — the human-readable, concatenated form a TLS stack consumes directly:
  a leaf+intermediates chain as one PEM blob, the private key as a second, and a
  bundle of trusted roots as a third.
* **JSON** — a structured envelope (base64-DER members) for APIs that prefer a
  single document, mirroring the shape SPIFFE's JWKS/bundle endpoints use.

All round-trips are loss-free for the fields that matter, and parsing is strict:
a chain that does not begin with a leaf carrying exactly one URI SAN is rejected
before it can be mistaken for an SVID.
"""

from __future__ import annotations

import base64
import json

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from app.zerotrust.identity.ca import TrustBundle
from app.zerotrust.identity.errors import CertificateError
from app.zerotrust.identity.keys import SigningKey
from app.zerotrust.identity.spiffe import TrustDomain
from app.zerotrust.identity.svid import X509Svid, spiffe_id_of_cert


def _split_pem_certs(pem: bytes) -> list[x509.Certificate]:
    try:
        return list(x509.load_pem_x509_certificates(pem))
    except ValueError as exc:
        raise CertificateError("could not parse PEM certificate chain") from exc


# --------------------------------------------------------------------------- #
# X.509-SVID <-> PEM
# --------------------------------------------------------------------------- #


def svid_to_pem(svid: X509Svid) -> tuple[bytes, bytes | None]:
    """Serialise an SVID to ``(chain_pem, key_pem_or_None)``.

    The chain is leaf-first, then intermediates — the wire order a TLS peer
    expects. The key is emitted only when the SVID carries one.
    """

    chain = svid.chain_pem()
    key_pem = svid.private_key.to_pem() if svid.private_key is not None else None
    return chain, key_pem


def svid_from_pem(chain_pem: bytes, key_pem: bytes | None = None) -> X509Svid:
    """Parse a leaf+intermediates chain (and optional key) back into an SVID."""

    certs = _split_pem_certs(chain_pem)
    if not certs:
        raise CertificateError("empty certificate chain")
    leaf, *intermediates = certs
    spiffe_id = spiffe_id_of_cert(leaf)  # validates the single URI SAN
    key = SigningKey.from_pem(key_pem) if key_pem is not None else None
    return X509Svid(spiffe_id, leaf, tuple(intermediates), key)


# --------------------------------------------------------------------------- #
# X.509-SVID <-> JSON envelope
# --------------------------------------------------------------------------- #


def svid_to_json(svid: X509Svid, *, include_key: bool = False) -> str:
    """Serialise an SVID to a JSON envelope (base64-DER members)."""

    doc: dict[str, object] = {
        "spiffe_id": svid.spiffe_id.uri,
        "leaf": base64.b64encode(svid.leaf.public_bytes(Encoding.DER)).decode("ascii"),
        "intermediates": [
            base64.b64encode(c.public_bytes(Encoding.DER)).decode("ascii")
            for c in svid.intermediates
        ],
    }
    if include_key and svid.private_key is not None:
        doc["key"] = base64.b64encode(svid.private_key.to_pem()).decode("ascii")
    return json.dumps(doc, separators=(",", ":"))


def svid_from_json(document: str) -> X509Svid:
    """Parse the JSON envelope produced by :func:`svid_to_json`."""

    try:
        doc = json.loads(document)
        leaf = x509.load_der_x509_certificate(base64.b64decode(doc["leaf"]))
        intermediates = tuple(
            x509.load_der_x509_certificate(base64.b64decode(c))
            for c in doc.get("intermediates", [])
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise CertificateError("malformed SVID JSON envelope") from exc
    spiffe_id = spiffe_id_of_cert(leaf)
    key_b64 = doc.get("key")
    key = SigningKey.from_pem(base64.b64decode(key_b64)) if key_b64 else None
    return X509Svid(spiffe_id, leaf, intermediates, key)


# --------------------------------------------------------------------------- #
# TrustBundle <-> PEM / JSON (federation)
# --------------------------------------------------------------------------- #


def trust_bundle_to_pem(bundle: TrustBundle, domain: str | TrustDomain) -> bytes:
    """Concatenated PEM of every trusted root for *domain*."""

    roots = bundle.roots_for(domain)
    if not roots:
        raise CertificateError(f"trust bundle has no roots for {domain}")
    return b"".join(c.public_bytes(Encoding.PEM) for c in roots)


def trust_bundle_to_json(bundle: TrustBundle) -> str:
    """Serialise every domain's roots into one federation document."""

    doc = {
        "trust_domains": {
            domain: [
                base64.b64encode(c.public_bytes(Encoding.DER)).decode("ascii")
                for c in bundle.roots_for(domain)
            ]
            for domain in sorted(bundle.domains())
        }
    }
    return json.dumps(doc, separators=(",", ":"))


def trust_bundle_from_json(document: str) -> TrustBundle:
    """Reconstruct a (possibly multi-domain / federated) trust bundle."""

    try:
        doc = json.loads(document)
        domains = doc["trust_domains"]
    except (KeyError, ValueError) as exc:
        raise CertificateError("malformed trust-bundle JSON document") from exc
    bundle = TrustBundle()
    for domain, roots_b64 in domains.items():
        for root_b64 in roots_b64:
            root = x509.load_der_x509_certificate(base64.b64decode(root_b64))
            bundle.add(domain, root)
    return bundle


def federate(*bundles: TrustBundle) -> TrustBundle:
    """Merge several trust bundles into one federated bundle.

    The result trusts roots from *every* input domain — the building block for a
    mesh that spans multiple trust domains (e.g. a partner deployment).
    """

    merged = TrustBundle()
    for b in bundles:
        merged.merge(b)
    return merged


__all__ = [
    "federate",
    "svid_from_json",
    "svid_from_pem",
    "svid_to_json",
    "svid_to_pem",
    "trust_bundle_from_json",
    "trust_bundle_to_json",
    "trust_bundle_to_pem",
]
