"""Identity: verify an Entra-shaped ID token and turn its groups claim into roles.

Offline, a dev IdP in `data/idp/` signs RS256 tokens with the same claim
shapes Entra ID v2 emits (`preferred_username`, `oid`, `groups` as object
ids). Live, `EntraVerifier` checks real tokens against the tenant's JWKS. The
code after verification is identical, which is the point: the gate never
learns which one it is talking to.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

from .policy import Policy


class IdentityError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    email: str
    name: str
    groups: tuple[str, ...]
    roles: tuple[str, ...]


def _ts(at: datetime) -> int:
    return int(at.timestamp())


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


class DevIdP:
    """Mints tokens for people in the seeded directory. Never use outside a demo."""

    def __init__(self, data: str | Path, policy: Policy):
        self.dir = Path(data) / "idp"
        self.policy = policy
        self.directory = {p["email"]: p for p in json.loads((self.dir / "directory.json").read_text(encoding="utf-8"))}

    def mint(self, email: str, at: datetime, ttl_s: int = 3600, groups: list[str] | None = None) -> str:
        person = self.directory[email]
        claims = {
            "iss": self.policy.identity["issuer"], "aud": self.policy.identity["audience"],
            "sub": person["oid"], "oid": person["oid"], "name": person["name"],
            "preferred_username": email, "groups": groups if groups is not None else person["groups"],
            "iat": _ts(at), "nbf": _ts(at), "exp": _ts(at + timedelta(seconds=ttl_s)),
        }
        key = (self.dir / "private.pem").read_bytes()
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "dev-1"})

    def jwks(self) -> dict:
        return json.loads((self.dir / "jwks.json").read_text(encoding="utf-8"))


def _identity_from_claims(claims: dict, policy: Policy) -> Identity:
    gclaim = policy.identity["groups_claim"]
    if gclaim not in claims and "groups" in claims.get("_claim_names", {}):
        # Entra's "groups overage": more than ~200 groups and the claim is replaced
        # by a pointer to Graph. Resolving it is out of scope; fail closed, don't guess.
        raise IdentityError("groups overage claim present; group membership not in token")
    groups = claims.get(gclaim) or []
    if not isinstance(groups, list):
        raise IdentityError("groups claim is not a list")
    email = claims.get(policy.identity["username_claim"])
    if not email:
        raise IdentityError("no username claim")
    roles = policy.roles_for_groups(groups)
    if not roles:
        raise IdentityError(f"none of the token's {len(groups)} group(s) maps to a role")
    return Identity(email=email, name=claims.get("name", email), groups=tuple(groups), roles=tuple(roles))


def verify(token: str, jwks: dict, policy: Policy, at: datetime) -> Identity:
    """Verify signature, issuer, audience and validity window *as of* `at`."""
    try:
        header = jwt.get_unverified_header(token)
        keys = {k["kid"]: k for k in jwks["keys"]}
        if header.get("alg") != "RS256" or header.get("kid") not in keys:
            raise IdentityError("token not signed by a known RS256 key")
        key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(keys[header["kid"]]))
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=policy.identity["audience"],
            issuer=policy.identity["issuer"], options={"verify_exp": False, "verify_nbf": False, "verify_iat": False},
        )
    except IdentityError:
        raise
    except jwt.PyJWTError as e:
        raise IdentityError(f"token rejected: {e}") from e
    t = _ts(at)
    if not (claims["nbf"] <= t < claims["exp"]):
        raise IdentityError("token not valid at the time the question was asked")
    return _identity_from_claims(claims, policy)


class EntraVerifier:
    """Live verification against an Entra ID tenant. Configured only by env var names:

    LENSGATE_ENTRA_TENANT_ID, LENSGATE_ENTRA_AUDIENCE
    """

    def __init__(self, policy: Policy):
        tenant = os.environ["LENSGATE_ENTRA_TENANT_ID"]
        self.policy = policy
        self.issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
        self.audience = os.environ["LENSGATE_ENTRA_AUDIENCE"]
        self.client = jwt.PyJWKClient(f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys")

    def verify(self, token: str) -> Identity:
        try:
            key = self.client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audience, issuer=self.issuer)
        except jwt.PyJWTError as e:
            raise IdentityError(f"token rejected: {e}") from e
        return _identity_from_claims(claims, self.policy)
