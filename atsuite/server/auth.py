"""Server-owned opaque token principals with explicit scope and expiration."""
from __future__ import annotations

import ipaddress
import re
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Mapping

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant: str
    project: str
    role: str
    audience: str = 'atsuite-control-plane'
    expires_at: float | None = None

    def __post_init__(self):
        for name in ('subject', 'tenant', 'project'):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
                raise ValueError(f'invalid principal {name}')
        if self.role not in {'viewer', 'editor', 'admin'}:
            raise ValueError('invalid principal role')

    def public(self):
        return asdict(self)


def loopback(host: str) -> bool:
    if host in {'localhost', 'testclient'}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class TokenIdentityProvider:
    def __init__(self, tokens: Mapping[str, Principal | dict] | None = None, *, mode='local_development'):
        if mode not in {'local_development', 'production'}:
            raise ValueError('mode must be local_development or production')
        self.mode = mode
        self.tokens = {token: principal if isinstance(principal, Principal) else Principal(**principal)
                       for token, principal in (tokens or {}).items()}
        if any(len(token) < 24 for token in self.tokens):
            raise ValueError('API tokens must contain at least 24 characters')
        if mode == 'production' and not any(principal.audience == 'atsuite-control-plane'
                and (principal.expires_at is None or principal.expires_at > time.time())
                for principal in self.tokens.values()):
            raise ValueError('production requires configured identity provider')

    def authenticate(self, request: Request) -> Principal:
        credential = request.headers.get('authorization', '')
        if self.tokens:
            for token, principal in self.tokens.items():
                if secrets.compare_digest(credential, 'Bearer ' + token):
                    if principal.audience != 'atsuite-control-plane':
                        raise HTTPException(401, 'invalid credential audience')
                    if principal.expires_at is not None and principal.expires_at <= time.time():
                        raise HTTPException(401, 'credential expired')
                    return principal
            raise HTTPException(401, 'invalid authorization')
        host = request.client.host if request.client else ''
        if not loopback(host) or not loopback(request.url.hostname or ''):
            raise HTTPException(403, 'local development accepts loopback requests only')
        # local users may run experiments, but production apply is never granted.
        return Principal('local-user', 'local', 'default', 'editor')

    def guard(self, request: Request, *, mutate=False, admin=False) -> Principal:
        principal = self.authenticate(request)
        if (mutate and principal.role not in {'editor', 'admin'}) or (admin and principal.role != 'admin'):
            raise HTTPException(403, 'required role is missing')
        if mutate:
            origin = request.headers.get('origin')
            if origin and origin != f'{request.url.scheme}://{request.url.netloc}':
                raise HTTPException(403, 'origin is not allowed')
        return principal
