"""Login/signup for the app.

Sign in with Apple has a single endpoint for both - it is idempotent, so the first call
for a given Apple account creates the user (signup) and every call after that is a
login. See app/auth/cognito_admin.py for why Cognito Admin APIs are used instead of
Cognito's own federated-IdP support.

Email + password accounts have /signup and /login; Cognito stores and checks the
user's own password, and the Cognito username is their real email.
"""

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.apple import AppleTokenValidationError, verify_apple_identity_token
from app.auth.cognito_admin import (
    AccountExistsError,
    CognitoAdminError,
    InvalidCredentialsError,
    WeakPasswordError,
    create_password_user,
    delete_user,
    ensure_cognito_user,
    global_sign_out,
    password_login,
    refresh_tokens,
    sign_in_user,
)
from app.auth.dev_token import mint_dev_token
from app.auth.google import GoogleTokenValidationError, verify_google_identity_token
from app.config import Settings, get_settings
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.auth import (
    AppleSignInRequest,
    DevTokenRequest,
    GoogleSignInRequest,
    LogoutRequest,
    PasswordLoginRequest,
    PasswordSignUpRequest,
    RefreshRequest,
    RefreshResponse,
    TokenResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _cognito_username_for(apple_sub: str) -> str:
    # This User Pool has "Username attributes" set to email, so AdminCreateUser
    # rejects any username that isn't email-shaped - a synthetic-but-validly-formatted
    # one, not a real deliverable address. Never used for actual email delivery: user
    # creation uses MessageAction=SUPPRESS and login goes through ADMIN_USER_PASSWORD_AUTH
    # with a backend-generated password (see cognito_admin.py), not anything
    # email-dependent like invites or password reset.
    return f"apple-{apple_sub}@users.aryapet.internal"


_INTERNAL_EMAIL_DOMAIN = "@users.aryapet.internal"


def _cognito_username_for_google(google_sub: str) -> str:
    return f"google-{google_sub}@users.aryapet.internal"


@router.post("/apple", response_model=TokenResponse)
async def sign_in_with_apple(
    payload: AppleSignInRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    try:
        claims = await verify_apple_identity_token(payload.identity_token, settings)
    except AppleTokenValidationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    apple_sub = claims["sub"]
    email = claims.get("email")
    now = datetime.now(UTC)

    result = await db.execute(select(User).where(User.apple_sub == apple_sub))
    user = result.scalar_one_or_none()
    is_new_user = user is None

    if user is None:
        cognito_username = _cognito_username_for(apple_sub)
        try:
            cognito_sub = await asyncio.to_thread(
                ensure_cognito_user, cognito_username, settings=settings
            )
        except CognitoAdminError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        user = User(
            apple_sub=apple_sub,
            cognito_username=cognito_username,
            cognito_sub=cognito_sub,
            email=email,
            first_name=payload.first_name,
            last_name=payload.last_name,
            last_login_at=now,
        )
        db.add(user)
        await db.flush()  # assigns user.id without committing yet
    else:
        user.last_login_at = now
        if email and not user.email:
            # Apple only sends email on the very first sign-in; capture it if we
            # somehow missed it (e.g. the row was created before this field existed).
            user.email = email
        # Same one-shot rule applies to the name — only ever backfill a name the
        # user doesn't already have, never overwrite one that's already stored.
        if payload.first_name and not user.first_name:
            user.first_name = payload.first_name
        if payload.last_name and not user.last_name:
            user.last_name = payload.last_name

    try:
        tokens = await asyncio.to_thread(sign_in_user, user.cognito_username, settings)
    except CognitoAdminError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    await db.commit()

    return TokenResponse(
        id_token=tokens.id_token,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        user_id=user.id,
        is_new_user=is_new_user,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
    )


def _token_response(tokens, user: User, *, is_new_user: bool) -> TokenResponse:
    return TokenResponse(
        id_token=tokens.id_token,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        user_id=user.id,
        is_new_user=is_new_user,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
    )


async def _apple_account_with_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(
        select(User).where(func.lower(User.email) == email, User.apple_sub.is_not(None))
    )
    return result.scalars().first()


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def sign_up_with_password(
    payload: PasswordSignUpRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """Creates an email + password account and signs it in.

    The address isn't verified by a code yet (Cognito's confirmation flow needs SES set
    up on the User Pool), so this proves only that the user knows the password they
    chose - not that they own the email.
    """
    email = payload.email.strip().lower()
    if email.endswith(_INTERNAL_EMAIL_DOMAIN):
        # The synthetic usernames minted for Apple/Google users live here.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use a real email address.")
    if await _apple_account_with_email(db, email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This email already has an account that uses Sign in with Apple.",
        )
    try:
        cognito_sub = await asyncio.to_thread(create_password_user, email, payload.password, settings)
    except AccountExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except WeakPasswordError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except CognitoAdminError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    try:
        tokens = await asyncio.to_thread(password_login, email, payload.password, settings)
        user = User(
            cognito_username=email,
            cognito_sub=cognito_sub,
            email=email,
            first_name=payload.first_name,
            last_name=payload.last_name,
            last_login_at=datetime.now(UTC),
        )
        db.add(user)
        await db.flush()
        await db.commit()
    except Exception as exc:
        # A Cognito user with no row here could never sign in, yet would block signing
        # up again with "already exists" - undo it.
        await db.rollback()
        await asyncio.to_thread(delete_user, email, settings)
        if isinstance(exc, CognitoAdminError):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        raise
    return _token_response(tokens, user, is_new_user=True)


@router.post("/login", response_model=TokenResponse)
async def log_in_with_password(
    payload: PasswordLoginRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    email = payload.email.strip().lower()
    result = await db.execute(select(User).where(User.cognito_username == email))
    user = result.scalar_one_or_none()
    if user is None:
        if await _apple_account_with_email(db, email) is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="This email uses Sign in with Apple. Tap Continue with Apple instead.",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password.")

    try:
        tokens = await asyncio.to_thread(password_login, email, payload.password, settings)
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except CognitoAdminError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    user.last_login_at = datetime.now(UTC)
    await db.commit()
    return _token_response(tokens, user, is_new_user=False)


@router.post("/google", response_model=TokenResponse)
async def sign_in_with_google(
    payload: GoogleSignInRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    try:
        claims = await verify_google_identity_token(payload.identity_token, settings)
    except GoogleTokenValidationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    google_sub = claims["sub"]
    email = claims.get("email")
    now = datetime.now(UTC)

    result = await db.execute(select(User).where(User.google_sub == google_sub))
    user = result.scalar_one_or_none()
    is_new_user = user is None

    if user is None:
        cognito_username = _cognito_username_for_google(google_sub)
        try:
            cognito_sub = await asyncio.to_thread(
                ensure_cognito_user, cognito_username, settings=settings
            )
        except CognitoAdminError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        user = User(
            google_sub=google_sub,
            cognito_username=cognito_username,
            cognito_sub=cognito_sub,
            email=email,
            first_name=payload.first_name,
            last_name=payload.last_name,
            last_login_at=now,
        )
        db.add(user)
        await db.flush()  # assigns user.id without committing yet
    else:
        user.last_login_at = now
        if email and not user.email:
            user.email = email
        if payload.first_name and not user.first_name:
            user.first_name = payload.first_name
        if payload.last_name and not user.last_name:
            user.last_name = payload.last_name

    try:
        tokens = await asyncio.to_thread(sign_in_user, user.cognito_username, settings)
    except CognitoAdminError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    await db.commit()

    return TokenResponse(
        id_token=tokens.id_token,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        user_id=user.id,
        is_new_user=is_new_user,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
    )


@router.post("/dev-token", response_model=TokenResponse)
async def issue_dev_token(
    payload: DevTokenRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """Mints a self-signed bearer token for a dev/test user, bypassing Cognito and
    Apple/Google entirely — for pasting into Swagger's Authorize button. Disabled
    unless DEV_AUTH_ENABLED is set (never in prod); see app/auth/dev_token.py."""
    if not settings.dev_auth_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    dev_sub = f"dev_{payload.subject}"
    now = datetime.now(UTC)

    result = await db.execute(select(User).where(User.cognito_sub == dev_sub))
    user = result.scalar_one_or_none()
    is_new_user = user is None

    if user is None:
        # Neither apple_sub nor google_sub set — a dev token has no real upstream
        # identity, just a Cognito-shaped identifier used to look the row back up.
        user = User(
            cognito_username=dev_sub,
            cognito_sub=dev_sub,
            email=payload.email,
            last_login_at=now,
        )
        db.add(user)
        await db.flush()
    else:
        user.last_login_at = now

    await db.commit()

    access_token = mint_dev_token(
        cognito_sub=dev_sub, client_id=settings.cognito_app_client_id, token_use="access"
    )
    id_token = mint_dev_token(cognito_sub=dev_sub, client_id=settings.cognito_app_client_id, token_use="id")

    return TokenResponse(
        id_token=id_token,
        access_token=access_token,
        refresh_token="dev-tokens-do-not-support-refresh",
        expires_in=3600,
        user_id=user.id,
        is_new_user=is_new_user,
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    payload: RefreshRequest,
    settings: Settings = Depends(get_settings),
) -> RefreshResponse:
    try:
        tokens = await asyncio.to_thread(refresh_tokens, payload.refresh_token, settings)
    except CognitoAdminError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    return RefreshResponse(
        id_token=tokens.id_token,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: LogoutRequest,
    settings: Settings = Depends(get_settings),
) -> None:
    try:
        await asyncio.to_thread(global_sign_out, payload.access_token, settings)
    except CognitoAdminError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
