from rest_framework.permissions import BasePermission
import logging


logger = logging.getLogger('billing')

class IsSuperuser(BasePermission):
    def has_permission(self, request, view):
        is_super = request.user.is_superuser
        logger.debug(f"Superuser check for user {request.user.id}: {is_super}")
        return is_super

class IsCEO(BasePermission):
    def has_permission(self, request, view):
        role = getattr(request.user, 'role', None)
        has_permission = bool(role and role.lower() == 'ceo')
        logger.debug(f"CEO check for user {getattr(request.user, 'id', None)}: {has_permission}")
        return has_permission

class IsCEOorSuperuser(BasePermission):
    def has_permission(self, request, view):
        role = getattr(request.user, 'role', None)
        has_permission = request.user.is_superuser or (role and role.lower() == 'ceo')
        logger.debug(f"CEO or Superuser check for user {getattr(request.user, 'id', None)}: {has_permission}")
        return has_permission

class CanViewEditSubscription(BasePermission):
    def has_object_permission(self, request, view, obj):
        role = getattr(request.user, 'role', None)
        if request.user.is_superuser or (role and role.lower() == 'superuser'):
            logger.debug(f"Subscription access granted for superuser {request.user.id}")
            return True
        tenant_id = getattr(request.user, 'tenant', None)
        has_permission = tenant_id and str(obj.tenant_id) == tenant_id and role and role.lower() == 'ceo'
        logger.debug(f"Subscription access check for user {request.user.id}, tenant {tenant_id}: {has_permission}")
        return has_permission

class PlanReadOnlyForCEO(BasePermission):
    def has_permission(self, request, view):
        role = getattr(request.user, 'role', None)
        if request.user.is_superuser or (role and role.lower() == 'superuser'):
            logger.debug(f"Full plan access granted for superuser {request.user.id}")
            return True
        if role and role.lower() == 'ceo':
            safe_methods = ['GET', 'HEAD', 'OPTIONS']
            has_permission = request.method in safe_methods
            logger.debug(f"Plan read-only check for CEO {request.user.id}: {has_permission}")
            return has_permission
        logger.debug(f"Plan access denied for user {request.user.id}")
        return False