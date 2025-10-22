from rest_framework.permissions import BasePermission

from apps.billing.utils import _extract_user_role



class IsSuperuser(BasePermission):
    def has_permission(self, request, view):
        is_super = request.user.is_superuser

        return is_super

class IsCEO(BasePermission):
    def has_permission(self, request, view):
        role = _extract_user_role(request.user)
        has_permission = role == 'ceo'

        return has_permission

class IsCEOorSuperuser(BasePermission):
    def has_permission(self, request, view):
        role = _extract_user_role(request.user)
        has_permission = request.user.is_superuser or role == 'ceo'

        return has_permission

class CanViewEditSubscription(BasePermission):
    def has_object_permission(self, request, view, obj):
        role = _extract_user_role(request.user)
        if request.user.is_superuser or role == 'superuser':

            return True
        tenant_id = getattr(request.user, 'tenant', None)
        has_permission = tenant_id and str(obj.tenant_id) == tenant_id and role == 'ceo'

        return has_permission

class PlanReadOnlyForCEO(BasePermission):
    def has_permission(self, request, view):
        role = _extract_user_role(request.user)
        if request.user.is_superuser or role == 'superuser':

            return True
        if role == 'ceo':
            safe_methods = ['GET', 'HEAD', 'OPTIONS']
            has_permission = request.method in safe_methods

            return has_permission

        return False