import requests
import logging
from django.conf import settings
from typing import Dict, Any, Optional

logger = logging.getLogger('billing')

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