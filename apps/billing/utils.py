import requests
import logging
from django.conf import settings
from typing import Dict, Any, Optional

logger = logging.getLogger('billing')

def _extract_user_role(user):
    role = getattr(user, 'user_role_lowercase', None)
    if not role and user.is_authenticated:
        role = user.groups.first().name.lower() if user.groups.exists() else None
    logger.debug(f"Extracted user role: {role} for user {user.id}")
    return role

class IdentityServiceClient:
    def __init__(self, request=None):
        self.request = request
        self.base_url = settings.IDENTITY_SERVICE_URL

    def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        try:
            headers = self._get_headers()
            response = requests.get(f"{self.base_url}/tenants/{tenant_id}", headers=headers, timeout=5)
            response.raise_for_status()
            logger.info(f"Tenant {tenant_id} retrieved from identity service")
            return response.json()
        except Exception as e:
            logger.error(f"Failed to get tenant {tenant_id}: {str(e)}")
            raise

    def get_users(self, tenant_id: str) -> list:
        try:
            headers = self._get_headers()
            response = requests.get(f"{self.base_url}/tenants/{tenant_id}/users", headers=headers, timeout=5)
            response.raise_for_status()
            logger.info(f"Users for tenant {tenant_id} retrieved from identity service")
            return response.json()
        except Exception as e:
            logger.error(f"Failed to get users for tenant {tenant_id}: {str(e)}")
            raise

    def get_branches(self, tenant_id: str) -> list:
        try:
            headers = self._get_headers()
            response = requests.get(f"{self.base_url}/tenants/{tenant_id}/branches", headers=headers, timeout=5)
            response.raise_for_status()
            logger.info(f"Branches for tenant {tenant_id} retrieved from identity service")
            return response.json()
        except Exception as e:
            logger.error(f"Failed to get branches for tenant {tenant_id}: {str(e)}")
            raise

    def _get_headers(self) -> Dict[str, str]:
        headers = {'Content-Type': 'application/json'}
        if self.request and hasattr(self.request, 'user') and self.request.user.is_authenticated:
            headers['Authorization'] = f"Bearer {self.request.user.auth_token}"
        return headers


from drf_yasg.utils import swagger_auto_schema
from .pagination import PAGINATION_PARAMS


def swagger_helper(tags, model):
    def decorators(func):
        descriptions = {
            "list": f"Retrieve a list of {model}",
            "retrieve": f"Retrieve details of a specific {model}",
            "create": f"Create a new {model}",
            "partial_update": f"Update a {model}",
            "destroy": f"Delete a {model}",
            "renew_subscription": f"Renew a {model}",
            "suspend_subscription": f"Suspend a {model}",
            "change_plan": f"Change plan for a {model}",
            "advance_renewal": f"Advance renewal for a {model}",
            "toggle_auto_renew": f"Toggle auto-renew for a {model}",
            "check_expired_subscriptions": f"Check expired {model}",
            "get_audit_logs": f"Get audit logs for a {model}",
            "get_subscription_details": f"Get {model} details",
            "get_analytics": f"Get analytics data",
            "list_subscriptions": f"List all {model}",
            "get_subscription_audit_logs": f"Get {model} audit logs",
            "retry_webhook": f"Retry webhook event",
            "list_webhook_events": f"List webhook events",
            "create_payment_summary": f"Create payment summary",
            "initiate_payment": f"Initiate payment",
            "confirm_payment": f"Confirm payment",
            "handle_webhook": f"Handle payment webhook",
            "refund_payment": f"Refund payment",
        }

        action_type = func.__name__
        get_description = descriptions.get(action_type, f"{action_type} {model}")
        return swagger_auto_schema(manual_parameters=PAGINATION_PARAMS, operation_id=f"{action_type} {model}", operation_description=get_description, tags=[tags])(func)

    return decorators



def generate_microservice_token(service_name="identity-ms", expires_in=300):
    """
    Generate a JWT token for microservice authentication.

    Args:
        service_name (str): Name of the requesting microservice
        expires_in (int): Token expiration in seconds (default: 5 minutes)

    Returns:
        str: JWT token
    """
    payload = {
        'type': 'microservice',
        'service': service_name,
        'iat': datetime.utcnow(),
        'exp': datetime.utcnow() + timedelta(seconds=expires_in)
    }

    token = jwt.encode(payload, settings.SUPPORT_JWT_SECRET_KEY, algorithm='HS256')
    return token


def send_email_via_service(email_data):
    """
    Send email through the email microservice.

    Args:
        email_data (dict): Email data including:
            - user_email (str): Recipient email (REQUIRED)
            - email_type (str): Type of email - must be one of: 'otp', 'confirmation', 'reset_link', 'general'
            - subject (str): Email subject (optional)
            - action (str): Action description (optional)
            - message (str): Email body (optional)
            - otp (str, optional): OTP code
            - link (str, optional): Action link
            - link_text (str, optional): Link display text

    Returns:
        dict: Response from email service
    """
    # Generate JWT token for microservice authentication
    token = generate_microservice_token()

    headers = {
        'Support-Microservice-Auth': token,
        'Content-Type': 'application/json'
    }
    support_service_url = settings.SUPPORT_MICROSERVICE_URL
    # Email service endpoint
    email_service_url = f"{support_service_url}/api/v1/email-service/send-email/"

    print(f"Sending email to {email_data['user_email']} via email service")
    print(f"Using URL: {email_service_url}")

    try:
        response = requests.post(
            email_service_url,
            json=email_data,
            headers=headers,
            timeout=30  # 30 seconds timeout
        )

        print(f"Email service response status: {response.status_code}")
        print(f"Email service response body: {response.text}")

        if response.status_code == 200:
            print("Email queued successfully")
            return response.json()
        else:
            print(f"Email service error: {response.text}")
            return {
                'error': 'Failed to send email',
                'status_code': response.status_code,
                'details': response.text
            }

    except requests.exceptions.RequestException as e:
        print(f"Request to email service failed: {str(e)}")
        return {
            'error': 'Connection to email service failed',
            'details': str(e)
        }
