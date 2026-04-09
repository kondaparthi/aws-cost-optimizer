"""
AWS Cognito authentication utilities for dashboard login.
Handles JWT token validation, refresh, and Cognito integration.
"""

import json
import os
import logging
import time
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timedelta

import boto3
import jwt
from jwt import PyJWTError
import requests
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Cognito configuration - will be set from CloudFormation outputs
COGNITO_REGION = os.environ.get('COGNITO_REGION', 'us-east-1')
COGNITO_USER_POOL_ID = os.environ.get('COGNITO_USER_POOL_ID')
COGNITO_CLIENT_ID = os.environ.get('COGNITO_CLIENT_ID')
COGNITO_CLIENT_SECRET = os.environ.get('COGNITO_CLIENT_SECRET')

# Initialize Cognito client
cognito_client = boto3.client('cognito-idp', region_name=COGNITO_REGION)


def get_cognito_public_keys() -> Dict[str, Any]:
    """
    Fetch Cognito User Pool public keys for JWT verification.

    Returns:
        Dictionary of public keys indexed by kid
    """
    try:
        # Get the JSON Web Key Set (JWKS) from Cognito
        jwks_url = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}/.well-known/jwks.json"
        response = requests.get(jwks_url)
        response.raise_for_status()

        jwks = response.json()
        keys = {}

        for key in jwks['keys']:
            kid = key['kid']
            keys[kid] = key

        return keys
    except Exception as e:
        logger.error(f"Failed to fetch Cognito public keys: {str(e)}")
        return {}


def verify_cognito_token(token: str, token_type: str = 'access') -> Optional[Dict[str, Any]]:
    """
    Verify and decode a Cognito JWT token.

    Args:
        token: JWT token to verify
        token_type: Type of token ('access', 'id', or 'refresh')

    Returns:
        Decoded token payload if valid, None otherwise
    """
    try:
        # Decode header to get kid
        header = jwt.get_unverified_header(token)
        kid = header.get('kid')

        if not kid:
            logger.warning("Token missing kid header")
            return None

        # Get public keys
        public_keys = get_cognito_public_keys()
        if kid not in public_keys:
            logger.warning(f"Unknown key ID: {kid}")
            return None

        # Get the public key
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(public_keys[kid]))

        # Verify and decode token
        decoded = jwt.decode(
            token,
            public_key,
            algorithms=['RS256'],
            audience=COGNITO_CLIENT_ID,
            issuer=f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
        )

        # Additional validation based on token type
        if token_type == 'access':
            if decoded.get('token_use') != 'access':
                logger.warning("Invalid token use for access token")
                return None
        elif token_type == 'id':
            if decoded.get('token_use') != 'id':
                logger.warning("Invalid token use for ID token")
                return None

        # Check expiration
        exp = decoded.get('exp')
        if exp and datetime.utcfromtimestamp(exp) < datetime.utcnow():
            logger.warning("Token has expired")
            return None

        return decoded

    except PyJWTError as e:
        logger.warning(f"JWT verification failed: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Token verification error: {str(e)}")
        return None


def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """
    Authenticate user with Cognito and return tokens.

    Args:
        username: User's username/email
        password: User's password

    Returns:
        Dictionary with access_token, id_token, refresh_token if successful, None otherwise
    """
    try:
        # Prepare authentication parameters
        auth_params = {
            'USERNAME': username,
            'PASSWORD': password
        }

        # Add client secret if configured
        if COGNITO_CLIENT_SECRET:
            import hashlib
            import hmac
            import base64

            message = username + COGNITO_CLIENT_ID
            dig = hmac.new(
                COGNITO_CLIENT_SECRET.encode('utf-8'),
                message.encode('utf-8'),
                hashlib.sha256
            ).digest()
            auth_params['SECRET_HASH'] = base64.b64encode(dig).decode()

        # Authenticate with Cognito
        response = cognito_client.admin_initiate_auth(
            UserPoolId=COGNITO_USER_POOL_ID,
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow='ADMIN_NO_SRP_AUTH',
            AuthParameters=auth_params
        )

        if 'AuthenticationResult' in response:
            auth_result = response['AuthenticationResult']
            return {
                'access_token': auth_result.get('AccessToken'),
                'id_token': auth_result.get('IdToken'),
                'refresh_token': auth_result.get('RefreshToken'),
                'expires_in': auth_result.get('ExpiresIn', 3600)
            }

        return None

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        if error_code in ('NotAuthorizedException', 'UserNotFoundException'):
            logger.warning(f"Authentication failed for user: {username} ({error_code})")
            return None
        logger.error(f"Cognito client error: {error_code}")
        return None
    except Exception as e:
        logger.error(f"Cognito authentication error: {str(e)}")
        return None


def refresh_access_token(refresh_token: str) -> Optional[Dict[str, Any]]:
    """
    Refresh access token using refresh token.

    Args:
        refresh_token: Valid refresh token

    Returns:
        Dictionary with new tokens if successful, None otherwise
    """
    try:
        auth_params = {
            'REFRESH_TOKEN': refresh_token
        }

        # Add client secret if configured
        if COGNITO_CLIENT_SECRET:
            import hashlib
            import hmac
            import base64

            message = COGNITO_CLIENT_ID
            dig = hmac.new(
                COGNITO_CLIENT_SECRET.encode('utf-8'),
                message.encode('utf-8'),
                hashlib.sha256
            ).digest()
            auth_params['SECRET_HASH'] = base64.b64encode(dig).decode()

        response = cognito_client.admin_initiate_auth(
            UserPoolId=COGNITO_USER_POOL_ID,
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow='REFRESH_TOKEN_AUTH',
            AuthParameters=auth_params
        )

        if 'AuthenticationResult' in response:
            auth_result = response['AuthenticationResult']
            return {
                'access_token': auth_result.get('AccessToken'),
                'id_token': auth_result.get('IdToken'),
                'expires_in': auth_result.get('ExpiresIn', 3600)
            }

        return None

    except Exception as e:
        logger.error(f"Token refresh error: {str(e)}")
        return None


def get_user_info(access_token: str) -> Optional[Dict[str, Any]]:
    """
    Get user information from access token.

    Args:
        access_token: Valid access token

    Returns:
        User information dictionary if successful, None otherwise
    """
    try:
        decoded = verify_cognito_token(access_token, 'access')
        if not decoded:
            return None

        # Extract user information
        return {
            'username': decoded.get('username'),
            'sub': decoded.get('sub'),
            'email': decoded.get('email'),
            'groups': decoded.get('cognito:groups', []),
            'token_use': decoded.get('token_use'),
            'scope': decoded.get('scope'),
            'exp': decoded.get('exp'),
            'iat': decoded.get('iat')
        }

    except Exception as e:
        logger.error(f"Failed to get user info: {str(e)}")
        return None


def validate_dashboard_access(user_info: Dict[str, Any]) -> bool:
    """
    Validate if user has access to the dashboard.

    Args:
        user_info: User information from token

    Returns:
        True if user has dashboard access, False otherwise
    """
    # Check if user is in required groups (customize as needed)
    allowed_groups = os.environ.get('ALLOWED_DASHBOARD_GROUPS', 'dashboard-users,admin').split(',')

    user_groups = user_info.get('groups', [])
    email = user_info.get('email', '')

    # Allow access if user is in any allowed group or has admin email
    for group in user_groups:
        if group.strip() in allowed_groups:
            return True

    # Allow specific admin emails (optional)
    admin_emails = os.environ.get('ADMIN_EMAILS', '').split(',')
    if email in admin_emails:
        return True

    return False


def create_secure_session_cookie(access_token: str, id_token: str, refresh_token: str, max_age: int = 3600) -> Dict[str, str]:
    """
    Create secure cookie with Cognito tokens.

    Args:
        access_token: Cognito access token
        id_token: Cognito ID token
        refresh_token: Cognito refresh token
        max_age: Cookie max age in seconds

    Returns:
        Headers dict with Set-Cookie
    """
    # Store tokens in a secure cookie (in production, consider server-side storage)
    session_data = {
        'access_token': access_token,
        'id_token': id_token,
        'refresh_token': refresh_token,
        'created_at': int(time.time())
    }

    # Encode session data (in production, encrypt this)
    import base64
    session_json = json.dumps(session_data)
    session_b64 = base64.b64encode(session_json.encode()).decode()

    return {
        'Set-Cookie': (
            f'cognito_session={session_b64}; '
            f'Max-Age={max_age}; '
            'Path=/; '
            'HttpOnly; '  # Prevent JavaScript access
            'Secure; '    # HTTPS only
            'SameSite=Strict'  # CSRF protection
        )
    }


def extract_tokens_from_cookie(cookie_value: str) -> Optional[Dict[str, str]]:
    """
    Extract tokens from session cookie.

    Args:
        cookie_value: Base64 encoded session data

    Returns:
        Dictionary with tokens if valid, None otherwise
    """
    try:
        import base64
        session_json = base64.b64decode(cookie_value).decode()
        session_data = json.loads(session_json)

        # Check if session is too old (24 hours max)
        created_at = session_data.get('created_at', 0)
        if time.time() - created_at > 86400:  # 24 hours
            return None

        return {
            'access_token': session_data.get('access_token'),
            'id_token': session_data.get('id_token'),
            'refresh_token': session_data.get('refresh_token')
        }

    except Exception as e:
        logger.error(f"Failed to extract tokens from cookie: {str(e)}")
        return None


def logout_user() -> Dict[str, str]:
    """
    Clear session cookies for logout.

    Returns:
        Headers dict with cookie clearing
    """
    return {
        'Set-Cookie': 'cognito_session=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Strict'
    }