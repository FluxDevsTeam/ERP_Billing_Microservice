import requests
from rest_framework import permissions


def _extract_role(user):
    possible_attrs = ['role', 'user_role', 'UserRole', 'Role']
    for attr in possible_attrs:
        value = getattr(user, attr, None)
        if isinstance(value, str):
            return value.lower()
    return None


class IsSuperuser(permissions.BasePermission):
    def has_permission(self, request, view):
        role_lc = _extract_role(request.user)
        return request.user and request.user.is_authenticated and (
            getattr(request.user, 'is_superuser', False) or role_lc == 'superuser'
        )


class IsCEO(permissions.BasePermission):
    def has_permission(self, request, view):
        role_lc = _extract_role(request.user)
        return request.user and request.user.is_authenticated and role_lc == 'ceo'


class CanInitiatePayment(permissions.BasePermission):
    def has_permission(self, request, view):
        role_lc = _extract_role(request.user)
        if getattr(request.user, 'is_superuser', False) or role_lc == 'superuser':
            return True
        if role_lc == 'ceo' and getattr(view, 'action', None) == 'create':
            # Ensure tenant context exists
            tenant_id = getattr(request.user, 'tenant', None)
            if isinstance(tenant_id, dict):
                tenant_id = tenant_id.get('id')
            return bool(tenant_id)
        return False


class CanViewPayment(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        role_lc = _extract_role(request.user)
        if getattr(request.user, 'is_superuser', False) or role_lc == 'superuser':
            return True
        # With current model (Payment -> Plan), we don't carry tenant context to enforce ownership safely.
        # Deny by default for CEOs and others to avoid leaking payment data.
        return False
