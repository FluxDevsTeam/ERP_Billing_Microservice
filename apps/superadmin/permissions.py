from rest_framework.permissions import BasePermission
import logging
logger = logging.getLogger('superadmin')


class IsSuperuser(BasePermission):
    def has_permission(self, request, view):
        is_super = request.user.is_superuser

        return is_super

class IsCEO(BasePermission):
    def has_permission(self, request, view):
        role = getattr(request.user, 'role', None)
        role = role.lower() if isinstance(role, str) else None
        logger.debug(f"superadmin.IsCEO.has_permission - user={getattr(request.user, 'id', None)} role_candidates={(role, getattr(request.user, 'role', None), getattr(request.user, 'user_role', None))}")
        has_permission = bool(role and role == 'ceo')
        return has_permission

class IsCEOorSuperuser(BasePermission):
    def has_permission(self, request, view):
        role = getattr(request.user, 'role', None)
        role = role.lower() if isinstance(role, str) else None
        logger.debug(f"superadmin.IsCEOorSuperuser.has_permission - user={getattr(request.user, 'id', None)} role_candidates={(role, getattr(request.user, 'role', None), getattr(request.user, 'user_role', None))}")
        has_permission = request.user.is_superuser or (role and role == 'ceo')
        return has_permission

class CanViewEditSubscription(BasePermission):
    def has_object_permission(self, request, view, obj):
        role = getattr(request.user, 'role', None)
        role = role.lower() if isinstance(role, str) else None
        if request.user.is_superuser or (role and role == 'superuser'):
            return True
        tenant_id = getattr(request.user, 'tenant', None)
        has_permission = tenant_id and str(obj.tenant_id) == tenant_id and role == 'ceo'
        logger.debug(f"superadmin.CanViewEditSubscription - user={getattr(request.user, 'id', None)} role={role} tenant={tenant_id} -> {has_permission}")
        return has_permission

class PlanReadOnlyForCEO(BasePermission):
    def has_permission(self, request, view):
        role = getattr(request.user, 'role', None)
        role = role.lower() if isinstance(role, str) else None
        if request.user.is_superuser or (role and role == 'superuser'):
            return True
        if role == 'ceo':
            safe_methods = ['GET', 'HEAD', 'OPTIONS']
            has_permission = request.method in safe_methods
            logger.debug(f"superadmin.PlanReadOnlyForCEO - user={getattr(request.user, 'id', None)} role={role} method={request.method} -> {has_permission}")
            return has_permission
        return False