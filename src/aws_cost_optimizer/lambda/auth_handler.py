"""
Lambda handler for Cognito authentication endpoints.
Provides login, validation, refresh, and logout functionality.
"""

import json
import logging
import os
from typing import Dict, Any, Optional
import boto3

from aws_cost_optimizer.utils.auth import (
    authenticate_user,
    verify_cognito_token,
    refresh_access_token,
    get_user_info,
    validate_dashboard_access,
    create_secure_session_cookie,
    extract_tokens_from_cookie,
    logout_user
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

sns_client = boto3.client('sns')


def _get_session_user(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return authenticated user info from session cookie, or None."""
    headers = event.get('headers', {}) or {}
    cookies = headers.get('Cookie') or headers.get('cookie') or ''
    if not cookies:
        return None

    session_cookie = None
    for cookie in cookies.split(';'):
        cookie = cookie.strip()
        if cookie.startswith('cognito_session='):
            session_cookie = cookie.split('=', 1)[1]
            break

    if not session_cookie:
        return None

    tokens = extract_tokens_from_cookie(session_cookie)
    if not tokens:
        return None

    user_info = get_user_info(tokens['access_token'])
    if not user_info:
        return None

    if not validate_dashboard_access(user_info):
        return None

    return user_info


def login_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Handle user login via Cognito.

    Expected body: {"username": "user@example.com", "password": "password"}
    """
    try:
        # Parse request body
        if 'body' not in event:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'Missing request body'})
            }

        body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
        username = body.get('username')
        password = body.get('password')

        if not username or not password:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'Username and password required'})
            }

        # Authenticate with Cognito
        tokens = authenticate_user(username, password)
        if not tokens:
            return {
                'statusCode': 401,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'Invalid credentials'})
            }

        # Verify access token and get user info
        user_info = get_user_info(tokens['access_token'])
        if not user_info:
            return {
                'statusCode': 401,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'Token validation failed'})
            }

        # Check dashboard access
        if not validate_dashboard_access(user_info):
            return {
                'statusCode': 403,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'Access denied'})
            }

        # Create secure session cookie
        cookie_headers = create_secure_session_cookie(
            tokens['access_token'],
            tokens['id_token'],
            tokens['refresh_token'],
            tokens.get('expires_in', 3600)
        )

        # Return success with user info (don't include tokens in response)
        response = {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                **cookie_headers
            },
            'body': json.dumps({
                'message': 'Login successful',
                'user': {
                    'username': user_info['username'],
                    'email': user_info.get('email'),
                    'groups': user_info.get('groups', [])
                }
            })
        }

        return response

    except json.JSONDecodeError:
        return {
            'statusCode': 400,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Invalid JSON'})
        }
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Internal server error'})
        }


def validate_session_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Validate current session and return user info.
    """
    try:
        # Get session cookie
        headers = event.get('headers', {}) or {}
        cookies = headers.get('Cookie') or headers.get('cookie') or ''
        if not cookies:
            return {
                'statusCode': 401,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'No session cookie'})
            }

        # Extract cognito_session cookie
        session_cookie = None
        for cookie in cookies.split(';'):
            cookie = cookie.strip()
            if cookie.startswith('cognito_session='):
                session_cookie = cookie.split('=', 1)[1]
                break

        if not session_cookie:
            return {
                'statusCode': 401,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'No session cookie'})
            }

        # Extract tokens from cookie
        tokens = extract_tokens_from_cookie(session_cookie)
        if not tokens:
            return {
                'statusCode': 401,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'Invalid session'})
            }

        # Verify access token
        user_info = get_user_info(tokens['access_token'])
        if not user_info:
            return {
                'statusCode': 401,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'Invalid token'})
            }

        # Check dashboard access
        if not validate_dashboard_access(user_info):
            return {
                'statusCode': 403,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'Access denied'})
            }

        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
                'user': {
                    'username': user_info['username'],
                    'email': user_info.get('email'),
                    'groups': user_info.get('groups', [])
                }
            })
        }

    except Exception as e:
        logger.error(f"Session validation error: {str(e)}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Internal server error'})
        }


def refresh_token_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Refresh access token using refresh token.
    """
    try:
        # Get session cookie
        headers = event.get('headers', {}) or {}
        cookies = headers.get('Cookie') or headers.get('cookie') or ''
        if not cookies:
            return {
                'statusCode': 401,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'No session cookie'})
            }

        # Extract cognito_session cookie
        session_cookie = None
        for cookie in cookies.split(';'):
            cookie = cookie.strip()
            if cookie.startswith('cognito_session='):
                session_cookie = cookie.split('=', 1)[1]
                break

        if not session_cookie:
            return {
                'statusCode': 401,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'No session cookie'})
            }

        # Extract tokens from cookie
        tokens = extract_tokens_from_cookie(session_cookie)
        if not tokens or not tokens.get('refresh_token'):
            return {
                'statusCode': 401,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'No refresh token'})
            }

        # Refresh tokens
        new_tokens = refresh_access_token(tokens['refresh_token'])
        if not new_tokens:
            return {
                'statusCode': 401,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'Token refresh failed'})
            }

        # Create new session cookie
        cookie_headers = create_secure_session_cookie(
            new_tokens['access_token'],
            new_tokens['id_token'],
            tokens['refresh_token'],  # Keep original refresh token
            new_tokens.get('expires_in', 3600)
        )

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                **cookie_headers
            },
            'body': json.dumps({'message': 'Token refreshed'})
        }

    except Exception as e:
        logger.error(f"Token refresh error: {str(e)}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Internal server error'})
        }


def logout_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Logout user by clearing session cookie.
    """
    try:
        cookie_headers = logout_user()

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                **cookie_headers
            },
            'body': json.dumps({'message': 'Logged out successfully'})
        }

    except Exception as e:
        logger.error(f"Logout error: {str(e)}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Internal server error'})
        }


def notify_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Send real email notifications through SNS for user-selected findings.

    Expected body:
    {
      "notifications": [{"id": "...", "type": "...", "issue": "...", "action": "notify"}],
      "dashboard_url": "https://..."
    }
    """
    try:
        user_info = _get_session_user(event)
        if not user_info:
            return {
                'statusCode': 401,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'Unauthorized'})
            }

        body = json.loads(event.get('body') or '{}') if isinstance(event.get('body'), str) else (event.get('body') or {})
        notifications = body.get('notifications') or []
        dashboard_url = body.get('dashboard_url') or ''

        if not notifications:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'No notifications provided'})
            }

        topic_arn = os.environ.get('SNS_TOPIC_ARN')
        if not topic_arn:
            return {
                'statusCode': 500,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'SNS topic is not configured'})
            }

        trimmed = notifications[:50]
        lines = []
        for item in trimmed:
            fid = item.get('id', 'unknown')
            ftype = item.get('type', 'unknown')
            issue = item.get('issue', 'No issue details provided')
            lines.append(f"- {fid} ({ftype}): {issue}")

        if len(notifications) > 50:
            lines.append(f"- ...and {len(notifications) - 50} more findings")

        actor = user_info.get('email') or user_info.get('username') or 'unknown-user'
        message = (
            "AWS Cost Optimizer - Notification Request\n\n"
            f"Requested by: {actor}\n"
            f"Notify findings count: {len(notifications)}\n"
            f"Dashboard URL: {dashboard_url or 'N/A'}\n\n"
            "Requested findings:\n"
            + "\n".join(lines)
        )

        sns_client.publish(
            TopicArn=topic_arn,
            Subject=f"[Cost Optimizer] User notify request ({len(notifications)} findings)",
            Message=message,
        )

        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
                'message': 'Notification email request sent',
                'sent_count': len(notifications)
            })
        }

    except json.JSONDecodeError:
        return {
            'statusCode': 400,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Invalid JSON'})
        }
    except Exception as e:
        logger.error(f"Notify handler error: {str(e)}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Internal server error'})
        }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler for authentication endpoints.
    Routes requests based on path and method.
    """
    try:
        # Enable CORS
        cors_headers = {
            'Access-Control-Allow-Origin': os.environ.get('CORS_ORIGIN', '*'),
            'Access-Control-Allow-Credentials': 'true',
            'Access-Control-Allow-Headers': 'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token',
            'Access-Control-Allow-Methods': 'GET,POST,OPTIONS'
        }

        # Handle preflight OPTIONS request
        if event.get('httpMethod') == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': cors_headers,
                'body': ''
            }

        # Route based on path
        path = event.get('path', '/')
        method = event.get('httpMethod', 'GET')

        if path == '/auth/login' and method == 'POST':
            response = login_handler(event, context)
        elif path == '/auth/validate' and method == 'GET':
            response = validate_session_handler(event, context)
        elif path == '/auth/refresh' and method == 'POST':
            response = refresh_token_handler(event, context)
        elif path == '/auth/logout' and method == 'POST':
            response = logout_handler(event, context)
        elif path == '/auth/notify' and method == 'POST':
            response = notify_handler(event, context)
        else:
            response = {
                'statusCode': 404,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'Endpoint not found'})
            }

        # Add CORS headers to response
        if 'headers' in response:
            response['headers'].update(cors_headers)
        else:
            response['headers'] = cors_headers

        return response

    except Exception as e:
        logger.error(f"Lambda handler error: {str(e)}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Internal server error'})
        }