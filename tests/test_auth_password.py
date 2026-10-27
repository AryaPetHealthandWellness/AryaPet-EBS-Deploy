"""Email + password sign-up and login (POST /v1/auth/signup, /v1/auth/login).

Cognito is replaced by an in-memory user pool that stores and checks passwords the way
the real one does, and the database by in-memory SQLite.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1 import auth as auth_routes
from app.auth.cognito_admin import (
    AccountExistsError,
    CognitoTokens,
    InvalidCredentialsError,
    WeakPasswordError,
)
from app.db.models.user import User
from app.db.session import get_db
from app.main import app


class FakeUserPool:
    def __init__(self) -> None:
        self.passwords: dict[str, str] = {}
        self.logins: list[str] = []

    def create(self, email: str, password: str, settings) -> str:
        if email in self.passwords:
            raise AccountExistsError("An account with this email already exists.")
        if len(password) < 8 or password.lower() == password:
            raise WeakPasswordError("Password must have uppercase characters")
        self.passwords[email] = password
        return f"sub-{email}"

    def login(self, email: str, password: str, settings) -> CognitoTokens:
        if self.passwords.get(email) != password:
            raise InvalidCredentialsError("Incorrect email or password.")
        self.logins.append(email)
        return CognitoTokens(
            id_token="id", access_token=f"access-{email}", refresh_token="refresh", expires_in=3600
        )

    def delete(self, email: str, settings) -> None:
        self.passwords.pop(email, None)


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> FakeUserPool:
    pool = FakeUserPool()
    monkeypatch.setattr(auth_routes, "create_password_user", pool.create)
    monkeypatch.setattr(auth_routes, "password_login", pool.login)
    monkeypatch.setattr(auth_routes, "delete_user", pool.delete)
    # Apple's password-resetting login must never run for an email account.
    monkeypatch.setattr(auth_routes, "sign_in_user", lambda *a, **k: pytest.fail("sign_in_user called"))
    return pool


@pytest.fixture
def client(pool):
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def db():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db
    with TestClient(app) as test_client:
        test_client.portal.call(_create_users_table, engine)
        test_client.engine = engine
        test_client.sessions = sessions
        yield test_client
    app.dependency_overrides.clear()


async def _create_users_table(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)


async def _add_apple_user(sessions, email: str) -> None:
    async with sessions() as session:
        session.add(
            User(
                id=uuid.uuid4(),
                apple_sub="apple-123",
                cognito_username="apple-123@users.aryapet.internal",
                cognito_sub="sub-apple",
                email=email,
            )
        )
        await session.commit()


def signup(client, email="Vet.Owner@Example.com", password="Paws4ever!", **extra):
    return client.post("/v1/auth/signup", json={"email": email, "password": password, **extra})


def login(client, email="vet.owner@example.com", password="Paws4ever!"):
    return client.post("/v1/auth/login", json={"email": email, "password": password})


def test_sign_up_then_log_in(client, pool):
    created = signup(client, first_name="Asha", last_name="Rao")
    assert created.status_code == 201
    body = created.json()
    assert body["is_new_user"] is True
    # Stored and matched case-insensitively.
    assert body["email"] == "vet.owner@example.com"
    assert body["first_name"] == "Asha"

    logged_in = login(client)
    assert logged_in.status_code == 200
    assert logged_in.json()["is_new_user"] is False
    assert logged_in.json()["user_id"] == body["user_id"]
    assert logged_in.json()["access_token"] == "access-vet.owner@example.com"


def test_wrong_password_is_refused(client, pool):
    signup(client)
    response = login(client, password="Wrong-pass1")
    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect email or password."


def test_unknown_email_gets_the_same_answer(client, pool):
    response = login(client, email="nobody@example.com")
    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect email or password."


def test_signing_up_twice_is_a_conflict(client, pool):
    signup(client)
    response = signup(client)
    assert response.status_code == 409


def test_a_weak_password_says_why(client, pool):
    response = signup(client, password="alllowercase")
    assert response.status_code == 400
    assert "uppercase" in response.json()["detail"]
    # Nothing left behind: the same email can still sign up.
    assert signup(client).status_code == 201


def test_too_short_or_malformed_input_is_rejected(client, pool):
    assert signup(client, password="short").status_code == 422
    assert signup(client, email="not-an-email").status_code == 422


def test_an_apple_account_email_is_pointed_at_apple(client, pool):
    client.portal.call(_add_apple_user, client.sessions, "owner@icloud.com")
    assert signup(client, email="owner@icloud.com").status_code == 409
    response = login(client, email="owner@icloud.com")
    assert response.status_code == 401
    assert "Sign in with Apple" in response.json()["detail"]


def test_internal_usernames_cannot_be_claimed(client, pool):
    response = signup(client, email="apple-123@users.aryapet.internal")
    assert response.status_code == 400
