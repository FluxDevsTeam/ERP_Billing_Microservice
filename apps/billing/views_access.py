# apps/billing/views_access.py
from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils import timezone
import uuid

from .models import Subscription
from .serializers import PlanSerializer
from .utils import swagger_helper


class AccessCheckView(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    @swagger_helper("Access Check", "list")
    def list(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                print("AccessCheckView.list: No tenant associated with user")
                return Response({
                    "access": False,
                    "message": "No tenant associated with user.",
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_403_FORBIDDEN)

            try:
                tenant_id = uuid.UUID(str(tenant_id))
            except ValueError:
                print("AccessCheckView.list: Invalid tenant ID format")
                return Response({
                    "access": False,
                    "message": "Invalid tenant ID format.",
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_400_BAD_REQUEST)

            try:
                subscription = Subscription.objects.select_related('plan').get(tenant_id=tenant_id)
            except Subscription.DoesNotExist:
                print("AccessCheckView.list: No subscription found for tenant")
                return Response({
                    "access": False,
                    "message": "No subscription found for tenant.",
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_403_FORBIDDEN)

            if subscription.status == 'active':
                access = True
                message = "Access granted"
            elif subscription.status == 'trial':
                access = True
                message = "Access granted (trial)"
            elif subscription.status == 'expired':
                if subscription.is_in_grace_period():
                    access = True
                    message = "Access granted (grace period)"
                else:
                    access = False
                    message = "Subscription expired"
            elif subscription.status == 'suspended':
                access = False
                message = "Subscription suspended"
            elif subscription.status == 'canceled':
                access = False
                message = "Subscription canceled"
            else:
                access = False
                message = f"Subscription {subscription.status}"

            response_data = {
                "access": access,
                "message": message,
                "tenant_id": str(tenant_id),
                "subscription_id": str(subscription.id),
                "plan": PlanSerializer(subscription.plan).data,
                "subscription_status": subscription.status,
                "expires_on": subscription.end_date.isoformat() if subscription.end_date else None,
                "remaining_days": subscription.get_remaining_days(),
                "in_grace_period": subscription.is_in_grace_period(),
                "auto_renew": subscription.tenant_billing_preferences.auto_renew_enabled if subscription.tenant_billing_preferences else False,  # Backwards compatibility
                "auto_renewal_active": subscription.tenant_billing_preferences.renewal_status == 'active' if subscription.tenant_billing_preferences else False,
                "timestamp": timezone.now().isoformat()
            }

            print(f"AccessCheckView.list: Access check for tenant_id={tenant_id}, access={access}")
            return Response(response_data, status=status.HTTP_200_OK if access else status.HTTP_403_FORBIDDEN)

        except Exception as e:
            print(f"AccessCheckView.list: Unexpected error - {str(e)}")
            return Response({
                "access": False,
                "message": "Access check failed",
                "error": str(e),
                "timestamp": timezone.now().isoformat()
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
