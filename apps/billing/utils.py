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