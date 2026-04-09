# AWS Cost Optimizer - Authentication Deployment Guide

This guide covers the deployment and configuration of secure Cognito-based authentication for the AWS Cost Optimizer dashboard.

## Overview

The authentication system uses AWS Cognito User Pools for secure user management and JWT tokens for session handling. The dashboard integrates with Cognito through API Gateway endpoints served via CloudFront.

## Architecture

```
User Browser → CloudFront → API Gateway → Lambda → Cognito User Pool
                      ↓
                S3 Static Files
```

## Components

### 1. Cognito User Pool
- **Purpose**: User authentication and authorization
- **Configuration**:
  - Email-based authentication
  - Password policies (configurable)
  - User groups for access control
  - JWT token generation

### 2. Authentication Lambda Function
- **Location**: `src/aws_cost_optimizer/lambda/auth_handler.py`
- **Endpoints**:
  - `POST /auth/login` - User login
  - `GET /auth/validate` - Session validation
  - `POST /auth/refresh` - Token refresh
  - `POST /auth/logout` - User logout

### 3. API Gateway
- **Purpose**: REST API for authentication endpoints
- **Configuration**: Regional deployment with CORS enabled

### 4. CloudFront Distribution
- **Purpose**: CDN and routing for dashboard and auth endpoints
- **Routes**:
  - `/` - Dashboard files (S3 origin)
  - `/auth/*` - Authentication API (API Gateway origin)

## Deployment Steps

### 1. Deploy with the Project Script

Use the project deployment script so Lambda package upload and code updates are handled consistently:

```bash
./scripts/deploy.sh \
  --stack-name cost-optimizer \
  --config-bucket your-config-bucket \
  --report-bucket your-report-bucket \
  --dashboard-bucket your-dashboard-bucket \
  --admin-email admin@example.com \
  --vpc-subnet-ids subnet-aaa,subnet-bbb \
  --vpc-security-group-ids sg-aaa,sg-bbb \
  --email ops@example.com
```

### 2. Lambda Code Packaging (Handled by script)

Package and upload the Lambda code:

```bash
# The deploy script builds a single package from src/aws_cost_optimizer
# and updates analysis, scheduler, and auth Lambda functions.
```

### 3. Cognito User Pool Setup

The deploy script bootstraps:
- `dashboard-users` group (created if missing)
- admin user from `--admin-email`
- admin user added to `dashboard-users`

On first sign-in, use the temporary password printed by the script and then set a new password.

If you prefer manual setup, use:

```bash
# Create admin user
aws cognito-idp admin-create-user \
  --user-pool-id $(aws cloudformation describe-stacks --stack-name cost-optimizer --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text) \
  --username admin@example.com \
  --temporary-password TempPass123! \
  --message-action SUPPRESS

# Set permanent password
aws cognito-idp admin-set-user-password \
  --user-pool-id $(aws cloudformation describe-stacks --stack-name cost-optimizer --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text) \
  --username admin@example.com \
  --password YourSecurePassword123! \
  --permanent
```

### 4. Add User to Dashboard Group

```bash
aws cognito-idp create-group \
  --group-name dashboard-users \
  --user-pool-id $(aws cloudformation describe-stacks --stack-name cost-optimizer --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text)

aws cognito-idp admin-add-user-to-group \
  --user-pool-id $(aws cloudformation describe-stacks --stack-name cost-optimizer --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text) \
  --username admin@example.com \
  --group-name dashboard-users
```

## Configuration

### Environment Variables

The Auth Lambda function uses these environment variables:

- `COGNITO_REGION`: AWS region (e.g., us-east-1)
- `COGNITO_USER_POOL_ID`: Cognito User Pool ID
- `COGNITO_CLIENT_ID`: Cognito User Pool Client ID
- `COGNITO_CLIENT_SECRET`: Cognito User Pool Client Secret (optional; not used when GenerateSecret=false)
- `ALLOWED_DASHBOARD_GROUPS`: Comma-separated list of allowed groups
- `CORS_ORIGIN`: Allowed CORS origin (CloudFront URL)

### Dashboard Configuration

The dashboard automatically detects the API Gateway URL through CloudFront routing. No additional configuration is required.

## Security Features

### Authentication
- JWT-based session management
- Secure cookie storage (HttpOnly, Secure, SameSite)
- Token expiration and refresh
- CSRF protection headers

### Authorization
- Group-based access control
- Email domain restrictions (optional)
- Session validation on each request

### Network Security
- HTTPS-only communication
- CORS protection
- API Gateway request throttling

## Testing

### Unit Tests

Run the authentication tests:

```bash
cd /Volumes/Macintosh HD - Data/git/aws-cost-optimizer
python -m pytest tests/test_auth_security.py -v
```

### Integration Testing

1. Access the dashboard URL from CloudFormation outputs
2. Attempt login with invalid credentials (should fail)
3. Login with valid Cognito credentials (should succeed)
4. Access dashboard without authentication (should redirect to login)
5. Test session persistence across browser refreshes

## Monitoring

### CloudWatch Logs

Monitor authentication logs:

```bash
# View Auth Lambda logs
aws logs tail /aws/lambda/cost-optimizer-auth-lambda --follow

# View API Gateway access logs
aws logs tail /aws/apigateway/cost-optimizer-auth-api --follow
```

### CloudWatch Metrics

Key metrics to monitor:
- Lambda invocation count and duration
- API Gateway 4xx/5xx error rates
- Cognito sign-in attempts and failures

## Troubleshooting

### Common Issues

1. **Login fails with "Invalid credentials"**
   - Verify user exists in Cognito User Pool
   - Check password policy compliance
   - Ensure user is in allowed groups

2. **CORS errors**
   - Verify CORS_ORIGIN environment variable matches CloudFront URL
   - Check API Gateway CORS configuration

3. **Token validation fails**
   - Verify JWT tokens are not expired
   - Check Cognito User Pool configuration
   - Validate public key fetching

4. **Dashboard redirects to login**
   - Check session cookie validity
   - Verify CloudFront distribution configuration
   - Ensure Lambda@Edge function is working

### Debug Commands

```bash
# Check Cognito User Pool status
aws cognito-idp describe-user-pool \
  --user-pool-id $(aws cloudformation describe-stacks --stack-name cost-optimizer --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text)

# Test Lambda function
aws lambda invoke \
  --function-name cost-optimizer-auth-lambda \
  --payload '{"body": "{\"username\":\"test@example.com\",\"password\":\"test\"}", "httpMethod": "POST", "path": "/auth/login"}' \
  output.json
```

## User Management

### Adding New Users

```bash
# Create user
aws cognito-idp admin-create-user \
  --user-pool-id <user-pool-id> \
  --username newuser@example.com \
  --temporary-password TempPass123!

# Set permanent password (user must change on first login)
aws cognito-idp admin-set-user-password \
  --user-pool-id <user-pool-id> \
  --username newuser@example.com \
  --password SecurePass123! \
  --permanent

# Add to dashboard group
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <user-pool-id> \
  --username newuser@example.com \
  --group-name dashboard-users
```

### Password Reset

Users can reset passwords through Cognito's built-in forgot password flow, or administrators can reset:

```bash
aws cognito-idp admin-reset-user-password \
  --user-pool-id <user-pool-id> \
  --username user@example.com
```

## Backup and Recovery

- Cognito User Pools are automatically backed up
- Lambda function code is stored in S3
- CloudFormation templates provide infrastructure recovery

## Cost Optimization

- API Gateway requests are charged per million requests
- Lambda invocations are charged per GB-second
- Cognito has free tier for first 50,000 monthly active users

## Security Best Practices

1. **Rotate Cognito Client Secrets** regularly
2. **Enable MFA** for admin users
3. **Monitor sign-in attempts** for suspicious activity
4. **Use least privilege IAM roles** for Lambda functions
5. **Enable CloudTrail** for audit logging
6. **Regular security updates** for Lambda runtime

## Support

For issues or questions:
1. Check CloudWatch logs for error details
2. Review CloudFormation stack events
3. Test with the provided unit tests
4. Refer to AWS documentation for Cognito and API Gateway