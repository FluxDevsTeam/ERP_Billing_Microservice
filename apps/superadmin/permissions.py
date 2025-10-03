from rest_framework.permissions import BasePermission
import logging

logger = logging.getLogger('billing')

def _extract_user_role(user):
    role = getattr(user, 'user_role_lowercase', None)
    if not role and user.is_authenticated:
        role = user.groups.first().name.lower() if user.groups.exists() else None
    logger.debug(f"Extracted user role: {role} for user {user.id}")
    return role

class IsSuperuser(BasePermission):
    def has_permission(self, request, view):
        is_super = request.user.is_superuser
        logger.debug(f"Superuser check for user {request.user.id}: {is_super}")
        return is_super

class IsCEO(BasePermission):
    def has_permission(self, request, view):
        role = _extract_user_role(request.user)
        has_permission = role == 'ceo'
        logger.debug(f"CEO check for user {request.user.id}: {has_permission}")
        return has_permission

class IsCEOorSuperuser(BasePermission):
    def has_permission(self, request, view):
        role = _extract_user_role(request.user)
        has_permission = request.user.is_superuser or role == 'ceo'
        logger.debug(f"CEO or Superuser check for user {request.user.id}: {has_permission}")
        return has_permission

class CanViewEditSubscription(BasePermission):
    def has_object_permission(self, request, view, obj):
        role = _extract_user_role(request.user)
        if request.user.is_superuser or role == 'superuser':
            logger.debug(f"Subscription access granted for superuser {request.user.id}")
            return True
        tenant_id = getattr(request.user, 'tenant', None)
        has_permission = tenant_id and str(obj.tenant_id) == tenant_id and role == 'ceo'
        logger.debug(f"Subscription access check for user {request.user.id}, tenant {tenant_id}: {has_permission}")
        return has_permission

class PlanReadOnlyForCEO(BasePermission):
    def has_permission(self, request, view):
        role = _extract_user_role(request.user)
        if request.user.is_superuser or role == 'superuser':
            logger.debug(f"Full plan access granted for superuser {request.user.id}")
            return True
        if role == 'ceo':
            safe_methods = ['GET', 'HEAD', 'OPTIONS']
            has_permission = request.method in safe_methods
            logger.debug(f"Plan read-only check for CEO {request.user.id}: {has_permission}")
            return has_permission
        logger.debug(f"Plan access denied for user {request.user.id}")
        return False