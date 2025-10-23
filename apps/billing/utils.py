import requests
import logging
from django.conf import settings
from typing import Dict, Any, Optional
from drf_yasg.utils import swagger_auto_schema
from .pagination import PAGINATION_PARAMS

logger = logging.getLogger('billing')

class IdentityServiceClient:
    def __init__(self, request=None):
        self.request = request
        self.base_url = settings.IDENTITY_MICROSERVICE_URL

    def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        try:
            headers = self._get_headers()
            response = requests.get(f"{self.base_url}/api/v1/tenant/{tenant_id}", headers=headers, timeout=5)
            response.raise_for_status()
            logger.info(f"Tenant {tenant_id} retrieved from identity service")
            return response.json()
        except Exception as e:
            logger.error(f"Failed to get tenant {tenant_id}: {str(e)}")
            raise

    def get_users(self, tenant_id: str) -> list:
        try:
            headers = self._get_headers()
            params = {'tenant_id': tenant_id} if tenant_id else None
            # identity service returns paginated results: {'count':.., 'results': [...]}
            response = requests.get(f"{self.base_url}/api/v1/user/management/", headers=headers, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
            results = data.get('results') if isinstance(data, dict) else None
            logger.info(f"Users for tenant {tenant_id} retrieved from identity service; count={data.get('count') if isinstance(data, dict) else 'unknown'}")
            return results if results is not None else (data if data is not None else [])
        except Exception as e:
            logger.error(f"Failed to get users for tenant {tenant_id}: {str(e)}")
            raise

    def get_branches(self, tenant_id: str) -> list:
        try:
            headers = self._get_headers()
            # some identity services expose branches at /api/v1/branch/ and support tenant filter
            params = {'tenant_id': tenant_id} if tenant_id else None
            # try branch endpoint first
            branch_url = f"{self.base_url}/api/v1/branch/"
            response = requests.get(branch_url, headers=headers, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
            results = data.get('results') if isinstance(data, dict) else None
            logger.info(f"Branches for tenant {tenant_id} retrieved from identity service; count={data.get('count') if isinstance(data, dict) else 'unknown'}")
            return results if results is not None else (data if data is not None else [])
        except Exception as e:
            logger.error(f"Failed to get branches for tenant {tenant_id}: {str(e)}")
            raise

    def _get_headers(self) -> Dict[str, str]:
        headers = {'Content-Type': 'application/json'}
        if self.request:
            # First try to get token from request headers
            auth_header = self.request.headers.get('Authorization')
            if auth_header:
                headers['Authorization'] = auth_header
                print(f"Using Authorization header from request: {auth_header[:15]}...")  # Debug print, only shows first 15 chars
            # Fallback to user object if no header found
            elif hasattr(self.request, 'user') and self.request.user.is_authenticated:
                access_token = getattr(self.request.user, 'access_token', None)
                if not access_token:
                    access_token = getattr(self.request.user, 'auth_token', None)
                
                if access_token:
                    headers['Authorization'] = f"JWT {access_token}"
                    print(f"Using Authorization header from user: JWT {access_token[:10]}...")
                else:
                    print("No access token found in request headers or user object")
        return headers



def get_request_role(request) -> Optional[str]:
    """Normalize and return a role string from the request or user object.

    Checks several common locations that identity systems use for storing role:
    - request.role
    - request.user.role
    - request.user.user_role
    - request.user.user_role_lowercase

    Returns the role as lowercase string or None if not found.
    """
    if not request:
        return None
    # direct request.role
    role = getattr(request, 'role', None)
    if role:
        return role.lower()
    # try user attributes
    user = getattr(request, 'user', None)
    if not user:
        return None
    for attr in ('role', 'user_role', 'user_role_lowercase'):
        if hasattr(user, attr):
            val = getattr(user, attr)
            if val:
                return val.lower() if isinstance(val, str) else None
    # sometimes role may be set under a nested dict like user.role['name'] etc. try best-effort
    try:
        val = getattr(user, 'role', None)
        if isinstance(val, dict) and 'name' in val:
            return str(val['name']).lower()
    except Exception:
        pass
    return None


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


