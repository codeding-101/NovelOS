from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.platform.config import PlatformSettings
from app.platform.models import AuditEvent, BrowserSession, LoginLimit, Owner, now

HASHER = PasswordHasher()
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))


def hashed(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def create_owner(session: Session, username: str, password: str) -> None:
    if not username.strip() or len(username) > 128 or not 12 <= len(password) <= 1024:
        raise ValueError(
            "Username is required; password must contain 12–1024 characters"
        )
    if session.get(Owner, "owner"):
        raise ValueError(
            "Owner already exists; initialization never overwrites an account"
        )
    session.add(
        Owner(
            id="owner", username=username.strip(), password_hash=HASHER.hash(password)
        )
    )
    session.add(
        AuditEvent(actor="bootstrap", action="owner.created", target_id="owner")
    )


def reserve_login_attempt(
    session: Session, settings: PlatformSettings, client: str
) -> bool:
    """Atomic increment before hashing; no in-memory rate limits or forwarded-IP trust."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy import case

    current = now()
    key = hashed(client)
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    statement = insert(LoginLimit).values(
        key=key,
        attempts=1,
        expires_at=current + timedelta(seconds=settings.login_window_seconds),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[LoginLimit.key],
        set_={
            "attempts": case(
                (LoginLimit.expires_at <= current, 1), else_=LoginLimit.attempts + 1
            ),
            "expires_at": case(
                (LoginLimit.expires_at <= current, statement.excluded.expires_at),
                else_=LoginLimit.expires_at,
            ),
        },
    ).returning(LoginLimit.attempts)
    attempts = session.execute(statement).scalar_one()
    session.commit()  # Reserve even if the subsequent password check fails.
    return attempts <= settings.login_limit


def login(session: Session, settings: PlatformSettings, username: str, password: str):
    owner = session.get(Owner, "owner")
    candidate_hash = owner.password_hash if owner else DUMMY_HASH
    try:
        valid = HASHER.verify(candidate_hash, password)
    except (VerificationError, InvalidHashError):
        valid = False
    if (
        not owner
        or not valid
        or not secrets.compare_digest(owner.username.encode(), username.encode())
    ):
        session.add(AuditEvent(actor="anonymous", action="login.failed"))
        session.commit()
        return None
    if HASHER.check_needs_rehash(owner.password_hash):
        owner.password_hash = HASHER.hash(password)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    session.execute(delete(BrowserSession).where(BrowserSession.expires_at <= now()))
    session.add(
        BrowserSession(
            token_hash=hashed(token),
            owner_id=owner.id,
            csrf_hash=hashed(csrf),
            expires_at=now() + timedelta(seconds=settings.session_seconds),
        )
    )
    session.add(AuditEvent(actor=owner.id, action="login.succeeded"))
    session.commit()
    return token, csrf


def authenticate(session: Session, token: str):
    record = session.get(BrowserSession, hashed(token)) if token else None
    if record is None or utc(record.expires_at) <= now():
        return None
    owner = session.scalar(select(Owner).where(Owner.id == record.owner_id))
    if owner is None:
        return None
    return {
        "id": owner.id,
        "username": owner.username,
        "csrf_hash": record.csrf_hash,
        "token_hash": record.token_hash,
    }


class CredentialVault:
    def __init__(self, master_key: str):
        self.cipher = Fernet(master_key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self.cipher.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self.cipher.decrypt(ciphertext.encode()).decode()
