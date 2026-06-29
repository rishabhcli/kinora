"""The :class:`IdentityFabric` facade — the one object a composition root wires.

The individual seams (issuer, verifier, KMS, secret store, policy) are useful on
their own, but most callers want them pre-wired against one trust domain and one
clock. :class:`IdentityFabric` is that bundle: build it once with
:meth:`bootstrap`, register workloads + policy, and hand it to the rest of the
system. It deliberately exposes the seams as attributes (not behind getters) so a
caller can reach the specific capability it needs while still sharing trust
material.

It also offers two end-to-end conveniences that compose the seams:

* :meth:`authorized_handshake` — verify a peer's SVID *and* policy-check the call
  in one step (the gate a server puts in front of a request);
* :meth:`authorize_token` — verify a JWT-SVID *and* policy-check it, the bearer
  analogue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.zerotrust.identity.attestation import AttestationResult
from app.zerotrust.identity.ca import TrustBundle
from app.zerotrust.identity.clock import Clock, SystemClock
from app.zerotrust.identity.issuer import IdentityIssuer, IssuedIdentity
from app.zerotrust.identity.jwt_svid import JwtSvidVerifier
from app.zerotrust.identity.keys import KeyAlgorithm, SigningKey
from app.zerotrust.identity.kms import LocalKms
from app.zerotrust.identity.mtls import SvidVerifier, VerifiedPeer
from app.zerotrust.identity.policy import AuthorizationPolicy, CallRequest, Decision
from app.zerotrust.identity.secrets import DynamicSecretEngine, SecretStore
from app.zerotrust.identity.spiffe import SpiffeId, TrustDomain
from app.zerotrust.identity.svid import JwtSvid, X509Svid

#: KEK id the fabric uses to seal its secret store.
DEFAULT_KEK_ID = "kinora-zerotrust-root-kek"


@dataclass(slots=True)
class IdentityFabric:
    """A pre-wired zero-trust identity + KMS + secrets + policy bundle."""

    trust_domain: TrustDomain
    clock: Clock
    issuer: IdentityIssuer
    kms: LocalKms
    secrets: SecretStore
    dynamic_secrets: DynamicSecretEngine
    policy: AuthorizationPolicy

    @classmethod
    def bootstrap(
        cls,
        trust_domain: str | TrustDomain,
        *,
        clock: Clock | None = None,
        algorithm: KeyAlgorithm = KeyAlgorithm.EC_P256,
        ca_key: SigningKey | None = None,
        jwt_key: SigningKey | None = None,
        kek_id: str = DEFAULT_KEK_ID,
        kek_material: bytes | None = None,
    ) -> IdentityFabric:
        """Stand up a complete fabric for *trust_domain*."""

        clk = clock or SystemClock()
        domain = (
            trust_domain
            if isinstance(trust_domain, TrustDomain)
            else TrustDomain(trust_domain)
        )
        issuer = IdentityIssuer.bootstrap(
            domain,
            clock=clk,
            ca_key=ca_key,
            jwt_key=jwt_key,
            algorithm=algorithm,
        )
        kms = LocalKms(clock=clk)
        kms.create_key(kek_id, material=kek_material)
        return cls(
            trust_domain=domain,
            clock=clk,
            issuer=issuer,
            kms=kms,
            secrets=SecretStore(kms=kms, key_id=kek_id, clock=clk),
            dynamic_secrets=DynamicSecretEngine(clock=clk),
            policy=AuthorizationPolicy(),
        )

    # -- trust material ---------------------------------------------------- #
    def trust_bundle(self) -> TrustBundle:
        return self.issuer.trust_bundle()

    def verifier(self, *, with_revocations: bool = True) -> SvidVerifier:
        """A fresh X.509-SVID verifier anchored at this fabric's trust bundle."""

        return SvidVerifier(
            self.trust_bundle(),
            clock=self.clock,
            revoked_serials=(
                self.issuer.ca.revoked_serials if with_revocations else frozenset()
            ),
        )

    def token_verifier(self) -> JwtSvidVerifier:
        return JwtSvidVerifier(self.issuer.jwt_registry(), clock=self.clock)

    # -- registration ------------------------------------------------------ #
    def register(
        self,
        spiffe_id: str,
        selectors: list[str] | None = None,
        *,
        svid_ttl: timedelta | None = None,
        downstream: list[str] | None = None,
    ) -> None:
        """Register a workload and (optionally) auto-add an allow rule for its
        declared downstreams."""

        from app.zerotrust.identity.ca import DEFAULT_LEAF_TTL
        from app.zerotrust.identity.policy import PolicyRule

        entry = self.issuer.registry.register(
            spiffe_id,
            selectors or [],
            svid_ttl=svid_ttl or DEFAULT_LEAF_TTL,
            downstream=downstream or [],
        )
        for target in entry.downstream:
            self.policy.add(
                PolicyRule.allow(
                    name=f"{entry.spiffe_id.path}->{target.path}",
                    callers=[entry.spiffe_id.uri],
                    targets=[target.uri],
                )
            )

    # -- end-to-end gates -------------------------------------------------- #
    def authorized_handshake(
        self,
        peer_svid: X509Svid,
        *,
        target: SpiffeId | str,
        action: str = "call",
        at: datetime | None = None,
    ) -> VerifiedPeer:
        """Verify *peer_svid* then policy-check (peer -> target [action]).

        Raises :class:`PeerVerificationError` on a bad cert and
        :class:`app.zerotrust.identity.errors.AuthorizationError` on policy DENY.
        """

        tgt = target if isinstance(target, SpiffeId) else SpiffeId.parse(target)
        peer = self.verifier().verify_peer(peer_svid, at=at)
        self.policy.authorize(
            CallRequest(caller=peer.spiffe_id, target=tgt, action=action)
        )
        return peer

    def authorize_token(
        self,
        token: str,
        *,
        target: SpiffeId | str,
        audience: str | None = None,
        action: str = "call",
    ) -> JwtSvid:
        """Verify a JWT-SVID then policy-check (sub -> target [action])."""

        tgt = target if isinstance(target, SpiffeId) else SpiffeId.parse(target)
        svid = self.token_verifier().verify(token, audience=audience)
        self.policy.authorize(
            CallRequest(caller=svid.spiffe_id, target=tgt, action=action)
        )
        return svid

    def decide(
        self, caller: SpiffeId | str, target: SpiffeId | str, action: str = "call"
    ) -> Decision:
        """Policy-only decision (no credential verification)."""

        c = caller if isinstance(caller, SpiffeId) else SpiffeId.parse(caller)
        t = target if isinstance(target, SpiffeId) else SpiffeId.parse(target)
        return self.policy.evaluate(CallRequest(caller=c, target=t, action=action))

    # -- issuance convenience --------------------------------------------- #
    def issue(self, attestation: AttestationResult) -> IssuedIdentity:
        return self.issuer.issue_for_attestation(attestation)


__all__ = ["DEFAULT_KEK_ID", "IdentityFabric"]
