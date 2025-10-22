from rest_framework import permissions
import logging

logger = logging.getLogger('payment')


class IsSuperuser(permissions.BasePermission):
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        role = getattr(request.user, 'role', None)
        role_lc = role.lower() if isinstance(role, str) else None
        is_super = getattr(request.user, 'is_superuser', False) or (role_lc == 'superuser')
        logger.debug(f"IsSuperuser.has_permission - user={getattr(request.user, 'id', None)} role={role_lc} -> {is_super}")
        return is_super


class IsCEO(permissions.BasePermission):
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        role = getattr(request.user, 'role', None)
        role_lc = role.lower() if isinstance(role, str) else None
        is_ceo = role_lc == 'ceo'
        logger.debug(f"IsCEO.has_permission - user={getattr(request.user, 'id', None)} role={role_lc} -> {is_ceo}")
        return is_ceo


class CanInitiatePayment(permissions.BasePermission):
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            logger.debug("CanInitiatePayment - User not authenticated")
            return False

        # Get user role with better error handling
        role = getattr(request.user, 'role', None)
        role_lc = role.lower() if isinstance(role, str) else None
        user_id = getattr(request.user, 'id', None)
        
        logger.debug(f"CanInitiatePayment - Checking permissions for user={user_id} role={role_lc}")
        
        # Superuser check
        if getattr(request.user, 'is_superuser', False) or role_lc == 'superuser':
            logger.debug(f"CanInitiatePayment - Superuser access granted")
            return True
            
        # CEO check - we don't need to check view action since this is specifically for payment initiation
        if role_lc == 'ceo':
            # Get tenant ID, handling both string and dict formats
            tenant_id = getattr(request.user, 'tenant', None)
            if isinstance(tenant_id, dict):
                tenant_id = tenant_id.get('id')
                
            # Log the tenant context
            logger.debug(f"CanInitiatePayment - CEO check: user={user_id} tenant_id={tenant_id}")
            
            # CEOs should be allowed to initiate payment regardless of tenant context
            logger.debug(f"CanInitiatePayment - CEO access granted")
            return True
            
        logger.debug(f"CanInitiatePayment - Permission denied for role={role_lc}")
        return False


class CanViewPayment(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        if not (request.user and request.user.is_authenticated):
            return False
        role = getattr(request.user, 'role', None)
        role_lc = role.lower() if isinstance(role, str) else None
        if getattr(request.user, 'is_superuser', False) or role_lc == 'superuser':
            return True
        logger.debug(f"CanViewPayment denied - user={getattr(request.user, 'id', None)} role={role_lc}")
        # With current model (Payment -> Plan), we don't carry tenant context to enforce ownership safely.
        # Deny by default for CEOs and others to avoid leaking payment data.
        return False
