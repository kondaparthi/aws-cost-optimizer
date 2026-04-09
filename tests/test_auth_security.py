"""
Tests for Cognito authentication utilities and Lambda handler.
"""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
import base64
import os
import importlib
import sys
from pathlib import Path
import time

# Ensure src/ is importable when tests run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

# Set test environment variables
os.environ['COGNITO_REGION'] = 'us-east-1'
os.environ['COGNITO_USER_POOL_ID'] = 'us-east-1_test'
os.environ['COGNITO_CLIENT_ID'] = 'test_client_id'
os.environ['COGNITO_CLIENT_SECRET'] = 'test_client_secret'

from aws_cost_optimizer.utils.auth import (
    verify_cognito_token,
    authenticate_user,
    refresh_access_token,
    get_user_info,
    validate_dashboard_access,
    create_secure_session_cookie,
    extract_tokens_from_cookie
)


class TestCognitoAuth:
    """Test Cognito authentication utilities."""

    @patch('aws_cost_optimizer.utils.auth.requests.get')
    def test_get_cognito_public_keys(self, mock_get):
        """Test fetching Cognito public keys."""
        mock_response = Mock()
        mock_response.json.return_value = {
            'keys': [
                {
                    'kid': 'test_kid',
                    'kty': 'RSA',
                    'n': 'test_n',
                    'e': 'AQAB'
                }
            ]
        }
        mock_get.return_value = mock_response

        from aws_cost_optimizer.utils.auth import get_cognito_public_keys
        keys = get_cognito_public_keys()

        assert 'test_kid' in keys
        mock_get.assert_called_once()

    def test_verify_cognito_token_invalid_header(self):
        """Test token verification with invalid header."""
        with patch('aws_cost_optimizer.utils.auth.get_cognito_public_keys', return_value={}):
            result = verify_cognito_token('invalid_token')
            assert result is None

    @patch('aws_cost_optimizer.utils.auth.jwt.decode')
    @patch('aws_cost_optimizer.utils.auth.get_cognito_public_keys')
    def test_verify_cognito_token_success(self, mock_get_keys, mock_decode):
        """Test successful token verification."""
        mock_get_keys.return_value = {'test_kid': {'kty': 'RSA', 'n': 'test', 'e': 'AQAB'}}
        mock_decode.return_value = {
            'sub': 'user123',
            'username': 'testuser',
            'token_use': 'access',
            'exp': 2000000000,
            'iat': 1000000000
        }

        with patch('aws_cost_optimizer.utils.auth.jwt.get_unverified_header') as mock_header:
            mock_header.return_value = {'kid': 'test_kid'}
            with patch('aws_cost_optimizer.utils.auth.jwt.algorithms', create=True) as mock_algs:
                mock_algs.RSAAlgorithm.from_jwk.return_value = 'mock_public_key'
                result = verify_cognito_token('valid_token')

                assert result is not None
                assert result['username'] == 'testuser'

    @patch('aws_cost_optimizer.utils.auth.cognito_client')
    def test_authenticate_user_success(self, mock_cognito):
        """Test successful user authentication."""
        mock_cognito.admin_initiate_auth.return_value = {
            'AuthenticationResult': {
                'AccessToken': 'access_token',
                'IdToken': 'id_token',
                'RefreshToken': 'refresh_token',
                'ExpiresIn': 3600
            }
        }

        result = authenticate_user('test@example.com', 'password123')

        assert result is not None
        assert result['access_token'] == 'access_token'
        mock_cognito.admin_initiate_auth.assert_called_once()

    @patch('aws_cost_optimizer.utils.auth.cognito_client')
    def test_authenticate_user_failure(self, mock_cognito):
        """Test user authentication failure."""
        mock_cognito.admin_initiate_auth.side_effect = Exception("Invalid credentials")

        result = authenticate_user('test@example.com', 'wrong_password')

        assert result is None

    def test_validate_dashboard_access_allowed_group(self):
        """Test dashboard access validation for allowed group."""
        user_info = {
            'username': 'testuser',
            'groups': ['dashboard-users'],
            'email': 'test@example.com'
        }

        result = validate_dashboard_access(user_info)
        assert result is True

    def test_validate_dashboard_access_denied(self):
        """Test dashboard access validation denial."""
        user_info = {
            'username': 'testuser',
            'groups': ['other-group'],
            'email': 'test@example.com'
        }

        result = validate_dashboard_access(user_info)
        assert result is False

    def test_create_secure_session_cookie(self):
        """Test secure session cookie creation."""
        headers = create_secure_session_cookie('access', 'id', 'refresh', 3600)

        assert 'Set-Cookie' in headers
        cookie_value = headers['Set-Cookie']
        assert 'cognito_session=' in cookie_value
        assert 'HttpOnly' in cookie_value
        assert 'Secure' in cookie_value
        assert 'SameSite=Strict' in cookie_value

    def test_extract_tokens_from_cookie(self):
        """Test token extraction from session cookie."""
        session_data = {
            'access_token': 'access',
            'id_token': 'id',
            'refresh_token': 'refresh',
            'created_at': int(time.time())
        }
        session_json = json.dumps(session_data)
        session_b64 = base64.b64encode(session_json.encode()).decode()

        result = extract_tokens_from_cookie(session_b64)

        assert result is not None
        assert result['access_token'] == 'access'

    def test_extract_tokens_from_expired_cookie(self):
        """Test token extraction from expired session cookie."""
        session_data = {
            'access_token': 'access',
            'id_token': 'id',
            'refresh_token': 'refresh',
            'created_at': 1000000000  # Old timestamp
        }
        session_json = json.dumps(session_data)
        session_b64 = base64.b64encode(session_json.encode()).decode()

        result = extract_tokens_from_cookie(session_b64)

        assert result is None


class TestAuthLambdaHandler:
    """Test Lambda authentication handler."""

    @patch('aws_cost_optimizer.lambda.auth_handler.authenticate_user')
    @patch('aws_cost_optimizer.lambda.auth_handler.get_user_info')
    @patch('aws_cost_optimizer.lambda.auth_handler.validate_dashboard_access')
    @patch('aws_cost_optimizer.lambda.auth_handler.create_secure_session_cookie')
    def test_login_handler_success(self, mock_create_cookie, mock_validate_access,
                                  mock_get_user, mock_authenticate):
        """Test successful login handler."""
        auth_handler = importlib.import_module('aws_cost_optimizer.lambda.auth_handler')
        login_handler = auth_handler.login_handler

        # Mock successful authentication
        mock_authenticate.return_value = {
            'access_token': 'access',
            'id_token': 'id',
            'refresh_token': 'refresh',
            'expires_in': 3600
        }
        mock_get_user.return_value = {
            'username': 'testuser',
            'email': 'test@example.com',
            'groups': ['dashboard-users']
        }
        mock_validate_access.return_value = True
        mock_create_cookie.return_value = {'Set-Cookie': 'session=cookie'}

        event = {
            'body': json.dumps({
                'username': 'test@example.com',
                'password': 'password123'
            })
        }

        result = login_handler(event, None)

        assert result['statusCode'] == 200
        assert 'Set-Cookie' in result['headers']

    def test_login_handler_invalid_request(self):
        """Test login handler with invalid request."""
        auth_handler = importlib.import_module('aws_cost_optimizer.lambda.auth_handler')
        login_handler = auth_handler.login_handler

        event = {'body': 'invalid json'}

        result = login_handler(event, None)

        assert result['statusCode'] == 400

    @patch('aws_cost_optimizer.lambda.auth_handler.extract_tokens_from_cookie')
    @patch('aws_cost_optimizer.lambda.auth_handler.get_user_info')
    @patch('aws_cost_optimizer.lambda.auth_handler.validate_dashboard_access')
    def test_validate_session_handler_success(self, mock_validate_access,
                                             mock_get_user, mock_extract_tokens):
        """Test successful session validation."""
        auth_handler = importlib.import_module('aws_cost_optimizer.lambda.auth_handler')
        validate_session_handler = auth_handler.validate_session_handler

        mock_extract_tokens.return_value = {'access_token': 'valid_token'}
        mock_get_user.return_value = {
            'username': 'testuser',
            'email': 'test@example.com',
            'groups': ['dashboard-users']
        }
        mock_validate_access.return_value = True

        event = {
            'headers': {'Cookie': 'cognito_session=valid_session'}
        }

        result = validate_session_handler(event, None)

        assert result['statusCode'] == 200
        response_body = json.loads(result['body'])
        assert response_body['user']['username'] == 'testuser'

    def test_validate_session_handler_no_cookie(self):
        """Test session validation without cookie."""
        auth_handler = importlib.import_module('aws_cost_optimizer.lambda.auth_handler')
        validate_session_handler = auth_handler.validate_session_handler

        event = {'headers': {}}

        result = validate_session_handler(event, None)

        assert result['statusCode'] == 401


if __name__ == '__main__':
    pytest.main([__file__])