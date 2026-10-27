"""Cognito Admin API calls used to mint session tokens for a user whose identity we've
already verified ourselves (via Sign in with Apple, see app/auth/apple.py).

Why this dance instead of Cognito's built-in federated-IdP support: federating Apple
directly in the Cognito User Pool routes auth through Cognito's Hosted UI (a browser
redirect), which doesn't fit a native iOS Sign in with Apple flow well and requires
Apple Developer + Cognito Console setup (Services ID, private key, Hosted UI domain)
that isn't in place yet. Instead, we treat Cognito purely as the token issuer:
  1. Verify Apple's token ourselves (app/auth/apple.py).
  2. Ensure a matching Cognito user exists (AdminCreateUser, first sign-in only).
  3. Reset that user's password to a fresh random value and immediately use it to log
     in (AdminSetUserPassword + AdminInitiateAuth). The password itself is never seen
     by the user or stored anywhere — it exists only to satisfy Cognito's requirement
     that ADMIN_USER_PASSWORD_AUTH have *some* credential to check.

This requires the User Pool App Client to have ALLOW_ADMIN_USER_PASSWORD_AUTH and
ALLOW_REFRESH_TOKEN_AUTH enabled, and be a *public* client (no client secret) — the app
client should not have a secret, since it's called from this backend on behalf of a
mobile app, and a secret provides no real protection for a backend whose credentials
would themselves need securing the same way. If the App Client does have a secret,
SECRET_HASH would need to be added to every call below.

IAM: the backend's execution role needs cognito-idp:AdminCreateUser,
cognito-idp:AdminSetUserPassword, cognito-idp:AdminInitiateAuth,
cognito-idp:AdminDeleteUser (undoing a failed email signup - best-effort, logged if
denied),
cognito-idp:InitiateAuth, and cognito-idp:GlobalSignOut on this User Pool's ARN. The
latter two back refresh_tokens()/global_sign_out(), which use the public (non-admin)
API operations — they're authorized primarily by the token being presented rather than
by caller identity, but boto3 still signs every call with the instance's IAM
credentials, so the role needs these actions allowed too.
"""

import logging
import secrets
import string
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

from app.config import Settings

logger = logging.getLogger(__name__)


class CognitoAdminError(Exception):
    pass


@dataclass
class CognitoTokens:
    id_token: str
    access_token: str
    refresh_token: str
    expires_in: int


def _client(settings: Settings):
    return boto3.client("cognito-idp", region_name=settings.cognito_region)


def _generate_password() -> str:
    """A random password that satisfies typical Cognito default policy (length +
    upper/lower/digit/symbol) — never stored or shown to the user."""
    alphabet_groups = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%^&*"]
    required = [secrets.choice(group) for group in alphabet_groups]
    remainder = [secrets.choice(string.ascii_letters + string.digits) for _ in range(16)]
    password = required + remainder
    secrets.SystemRandom().shuffle(password)
    return "".join(password)


def _extract_sub(attributes: list[dict[str, str]]) -> str:
    for attr in attributes:
        if attr["Name"] == "sub":
            return attr["Value"]
    raise CognitoAdminError("Cognito user has no sub attribute")


def ensure_cognito_user(username: str, *, settings: Settings) -> str:
    """Creates the Cognito user if they don't already exist. Idempotent. Returns
    Cognito's own internal user id (the `sub` claim value) either way."""
    client = _client(settings)
    # This User Pool has Username attributes = email, which means Cognito requires
    # the "email" user attribute to either be omitted or exactly equal the username -
    # it can't hold the real Apple/Google email (that's already stored in our own
    # users table; Cognito never needs it, since login is backend-controlled
    # ADMIN_USER_PASSWORD_AUTH, not anything email-dependent like invites or resets).
    user_attributes = [
        {"Name": "email", "Value": username},
        {"Name": "email_verified", "Value": "true"},
    ]

    try:
        response = client.admin_create_user(
            UserPoolId=settings.cognito_user_pool_id,
            Username=username,
            UserAttributes=user_attributes,
            MessageAction="SUPPRESS",  # no invite email — this user never sets/knows a password
        )
        return _extract_sub(response["User"]["Attributes"])
    except client.exceptions.UsernameExistsException:
        response = client.admin_get_user(UserPoolId=settings.cognito_user_pool_id, Username=username)
        return _extract_sub(response["UserAttributes"])
    except ClientError as exc:
        raise CognitoAdminError(f"Failed to create Cognito user: {exc}") from exc


def _admin_password_login(username: str, password: str, settings: Settings) -> CognitoTokens:
    client = _client(settings)

    client.admin_set_user_password(
        UserPoolId=settings.cognito_user_pool_id,
        Username=username,
        Password=password,
        Permanent=True,
    )

    try:
        response = client.admin_initiate_auth(
            UserPoolId=settings.cognito_user_pool_id,
            ClientId=settings.cognito_app_client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
    except ClientError as exc:
        raise CognitoAdminError(f"Failed to authenticate with Cognito: {exc}") from exc

    result = response["AuthenticationResult"]
    return CognitoTokens(
        id_token=result["IdToken"],
        access_token=result["AccessToken"],
        refresh_token=result["RefreshToken"],
        expires_in=result["ExpiresIn"],
    )


def sign_in_user(username: str, settings: Settings) -> CognitoTokens:
    """Mints fresh Cognito tokens for an existing (or just-created) user."""
    password = _generate_password()
    return _admin_password_login(username, password, settings)


def refresh_tokens(refresh_token: str, settings: Settings) -> CognitoTokens:
    """Uses the plain (non-admin) InitiateAuth — the refresh token alone identifies the
    session, so this doesn't need admin credentials or the username."""
    client = _client(settings)
    try:
        response = client.initiate_auth(
            ClientId=settings.cognito_app_client_id,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": refresh_token},
        )
    except ClientError as exc:
        raise CognitoAdminError(f"Failed to refresh tokens: {exc}") from exc

    result: dict[str, Any] = response["AuthenticationResult"]
    return CognitoTokens(
        id_token=result["IdToken"],
        access_token=result["AccessToken"],
        # Cognito doesn't rotate the refresh token on REFRESH_TOKEN_AUTH; the caller
        # keeps using the same one until it expires.
        refresh_token=refresh_token,
        expires_in=result["ExpiresIn"],
    )


def global_sign_out(access_token: str, settings: Settings) -> None:
    """Revokes all of this user's outstanding refresh tokens (logout). Uses the plain
    (non-admin) GlobalSignOut, authorized by the caller's own access token — no admin
    credentials needed, and it can't be used to sign out a different user."""
    client = _client(settings)
    try:
        client.global_sign_out(AccessToken=access_token)
    except ClientError as exc:
        raise CognitoAdminError(f"Failed to sign out: {exc}") from exc


# ── Email + password accounts ──────────────────────────────────────────────
#
# Unlike Apple users, these users know their password: Cognito stores and checks it.
# Their username is their real email. Never pass one to sign_in_user() above - it
# would overwrite their password with a random one.


class InvalidCredentialsError(CognitoAdminError):
    """Wrong email or password. Deliberately doesn't say which."""


class AccountExistsError(CognitoAdminError):
    pass


class WeakPasswordError(CognitoAdminError):
    """The password fails the User Pool's policy; the message says why."""


def _tokens(response: dict[str, Any]) -> CognitoTokens:
    result = response["AuthenticationResult"]
    return CognitoTokens(
        id_token=result["IdToken"],
        access_token=result["AccessToken"],
        refresh_token=result["RefreshToken"],
        expires_in=result["ExpiresIn"],
    )


def create_password_user(email: str, password: str, settings: Settings) -> str:
    """Creates an email + password user with the user's own password and returns its
    Cognito `sub`. No invite email: the user just chose the password."""
    client = _client(settings)
    try:
        response = client.admin_create_user(
            UserPoolId=settings.cognito_user_pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                # Not yet verified by a code - see the signup route.
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )
    except client.exceptions.UsernameExistsException as exc:
        raise AccountExistsError("An account with this email already exists.") from exc
    except ClientError as exc:
        raise CognitoAdminError(f"Failed to create Cognito user: {exc}") from exc

    try:
        client.admin_set_user_password(
            UserPoolId=settings.cognito_user_pool_id,
            Username=email,
            Password=password,
            Permanent=True,
        )
    except client.exceptions.InvalidPasswordException as exc:
        # Don't leave a user behind that can never sign in.
        _try_delete(client, email, settings)
        raise WeakPasswordError(exc.response["Error"]["Message"]) from exc
    except ClientError as exc:
        _try_delete(client, email, settings)
        raise CognitoAdminError(f"Failed to set password: {exc}") from exc
    return _extract_sub(response["User"]["Attributes"])


def password_login(email: str, password: str, settings: Settings) -> CognitoTokens:
    """Checks the user's own password and returns their session tokens."""
    client = _client(settings)
    try:
        response = client.admin_initiate_auth(
            UserPoolId=settings.cognito_user_pool_id,
            ClientId=settings.cognito_app_client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )
    except (client.exceptions.NotAuthorizedException, client.exceptions.UserNotFoundException) as exc:
        raise InvalidCredentialsError("Incorrect email or password.") from exc
    except ClientError as exc:
        raise CognitoAdminError(f"Failed to authenticate with Cognito: {exc}") from exc
    return _tokens(response)


def delete_user(username: str, settings: Settings) -> None:
    """Removes a Cognito user - used to undo a signup that failed partway.
    Best-effort: a failure is logged, never raised over the error being reported."""
    _try_delete(_client(settings), username, settings)


def _try_delete(client, username: str, settings: Settings) -> None:
    try:
        client.admin_delete_user(UserPoolId=settings.cognito_user_pool_id, Username=username)
    except ClientError as exc:
        logger.warning("Could not delete Cognito user %s after a failed signup: %s", username, exc)
