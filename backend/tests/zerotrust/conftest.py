"""Deterministic fixtures for the zero-trust identity + KMS suite.

Fixed PEM keys + a fixed/manual clock make the crypto tests reproducible:
ECDSA signatures are still randomised (so we assert *verification*, never
signature byte-equality), but the **key material** and **time** are pinned so
certificate serials/windows and rotation timing are exactly controllable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.zerotrust.identity import (
    IdentityFabric,
    KeyAlgorithm,
    ManualClock,
    SigningKey,
)

# --------------------------------------------------------------------------- #
# Fixed key fixtures (deterministic crypto material).
# --------------------------------------------------------------------------- #
EC_CA_PEM = b"""-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgEmesBhzqpub3rnmB
n4t9b0UOp9wjDFzkdIGpap+38MWhRANCAAQp0eTZb+fbGv1DeLb66gkiKk6a0Hos
VN8MCGP1ZMg5EjWZLyhqJYwgbcf0RE0IxegxsgTOUJS1ye/4905Qvwql
-----END PRIVATE KEY-----
"""

EC_JWT_PEM = b"""-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgHRd88sbd2xYLVl9T
SVrsiYU1MUTc/v3dGGfpJDSZsmShRANCAAT+lq+vTAXpe5+rvCVCLXSfttXYASQX
lvynUk1UsJOYimdKkcsqVCGDQ30LpnXK6vK5jvicORPeEGXtzEd5KGOM
-----END PRIVATE KEY-----
"""

ED_CA_PEM = b"""-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEID1urKPhkzKzz3Bn5A2TXfHCN4YGKV5Np/sitqw4fFgp
-----END PRIVATE KEY-----
"""

#: A fixed AES-256 KEK so wrap/unwrap fixtures are reproducible.
FIXED_KEK = bytes(range(32))

#: The fixed instant the suite anchors at.
EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

TRUST_DOMAIN = "acme.kinora.internal"


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(EPOCH)


@pytest.fixture
def ec_ca_key() -> SigningKey:
    return SigningKey.from_pem(EC_CA_PEM)


@pytest.fixture
def ec_jwt_key() -> SigningKey:
    return SigningKey.from_pem(EC_JWT_PEM)


@pytest.fixture
def ed_ca_key() -> SigningKey:
    return SigningKey.from_pem(ED_CA_PEM)


@pytest.fixture
def fabric(clock: ManualClock, ec_ca_key: SigningKey, ec_jwt_key: SigningKey) -> IdentityFabric:
    """A fully bootstrapped fabric on fixed keys + a manual clock."""

    return IdentityFabric.bootstrap(
        TRUST_DOMAIN,
        clock=clock,
        algorithm=KeyAlgorithm.EC_P256,
        ca_key=ec_ca_key,
        jwt_key=ec_jwt_key,
        kek_material=FIXED_KEK,
    )
