# apps/billing/views_subscription.py
from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.filters import SearchFilter
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.core.exceptions import ValidationError
from django.utils import timezone
import uuid
import logging
from django_filters import rest_framework as filters

logger = logging.getLogger(__name__)

from .models import Plan, Subscription, AuditLog, TenantBillingPreferences
from apps.payment.models import Payment
from .serializers import (
    SubscriptionSerializer, PaymentSerializer,
    SubscriptionCreateSerializer, TrialActivationSerializer,
    SubscriptionSuspendSerializer, AuditLogSerializer
)
from .permissions import IsSuperuser, IsCEOorSuperuser, CanViewEditSubscription
from .utils import IdentityServiceClient, swagger_helper
from .services import SubscriptionService
from api.email_service import send_email_via_service


class SubscriptionFilter(filters.FilterSet):
    auto_renew = filters.BooleanFilter(field_name='tenant_billing_preferences__auto_renew_enabled')

    class Meta:
        model = Subscription
        fields = ['tenant_id', 'plan', 'status']


class SubscriptionView(viewsets.ModelViewSet):
    queryset = Subscription.objects.all()
    filter_backends = [DjangoFilterBackend, SearchFilter]
    search_fields = ['tenant_id']
    filterset_class = SubscriptionFilter

    def get_queryset(self):
        user = self.request.user
        role = getattr(user, 'role', None)
        print(f"SubscriptionView.get_queryset - User: {user}, Role: {role}")
        if user.is_superuser or (role and role.lower() == 'superuser'):
            print("SubscriptionView: Superuser accessing all subscriptions")
            return Subscription.objects.select_related('plan', 'scheduled_plan').all()
        tenant_id = getattr(user, 'tenant', None)
        print(f"SubscriptionView: Tenant ID: {tenant_id}")
        if tenant_id and role and role.lower() == 'ceo':
            try:
                tenant_id = uuid.UUID(str(tenant_id))
                print(f"SubscriptionView: Filtering subscriptions for tenant_id={tenant_id}")
                return Subscription.objects.select_related('plan', 'scheduled_plan').filter(tenant_id=tenant_id)
            except ValueError:
                print("SubscriptionView: Invalid tenant ID format")
                return Subscription.objects.none()
        print("SubscriptionView: No relevant role or tenant, returning empty queryset")
        return Subscription.objects.none()

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [IsAuthenticated(), IsCEOorSuperuser()]
        if self.action in ['create', 'update', 'partial_update', 'suspend_subscription', 'activate_trial']:
            return [IsAuthenticated(), CanViewEditSubscription()]
        if self.action == 'destroy':
            return [IsAuthenticated(), IsSuperuser()]
        return [IsAuthenticated()]

    def get_serializer_class(self):
        if self.action == 'create':
            return SubscriptionCreateSerializer
        if self.action == 'activate_trial':
            return TrialActivationSerializer
        if self.action == 'suspend_subscription':
            return SubscriptionSuspendSerializer
        if self.action == 'get_audit_logs':
            return AuditLogSerializer
        return SubscriptionSerializer

    @swagger_helper("Subscriptions", "create_subscription")
    def create(self, request, *args, **kwargs):
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            tenant_id = serializer.validated_data['tenant_id']
            plan_id = serializer.validated_data['plan_id']

            # Check if this is a trial activation (when auto_renew is not explicitly set to False and no existing subscription)
            is_trial = serializer.validated_data.get('is_trial', False)
            machine_number = request.data.get('machine_number')
            
            subscription, result = subscription_service.create_subscription(
                tenant_id=str(tenant_id),
                plan_id=str(plan_id),
                user=str(request.user.id),
                is_trial=is_trial
            )

            # Create/update tenant billing preferences
            auto_renew = serializer.validated_data.get('auto_renew', True)
            preferences, created = TenantBillingPreferences.objects.get_or_create(
                tenant_id=tenant_id,
                defaults={
                    'user_id': str(request.user.id),
                    'auto_renew_enabled': auto_renew,
                    'preferred_plan_id': plan_id,
                    'subscription_expiry_date': subscription.end_date,
                    'next_renewal_date': subscription.end_date if auto_renew else None,
                }
            )
            if not created:
                # Update existing preferences
                preferences.auto_renew_enabled = auto_renew
                preferences.preferred_plan_id = plan_id
                preferences.subscription_expiry_date = subscription.end_date
                preferences.next_renewal_date = subscription.end_date if auto_renew else None
                preferences.save()

            # Get tenant CEO email for notification
            ceo_email = None
            try:
                client = IdentityServiceClient(request=request)
                users = client.get_users(tenant_id=str(tenant_id))
                if users and isinstance(users, list):
                    # Find the first user with role 'ceo'
                    ceo_user = next((user for user in users if user.get('role') == 'ceo'), None)
                    if ceo_user:
                        ceo_email = ceo_user.get('email')
                print(f"SubscriptionView.create: Tenant CEO email - {ceo_email}")
            except Exception as e:
                print(f"SubscriptionView.create: Error fetching tenant users - {str(e)}")
                ceo_email = None

            # Send welcome email to tenant CEO
            if ceo_email:
                email_data = {
                    'user_email': ceo_email,
                    'email_type': 'confirmation',
                    'subject': 'Welcome to Your New Subscription',
                    'message': f'Your subscription to {subscription.plan.name} plan has been created successfully.',
                    'action': 'Subscription Created'
                }
                send_email_via_service(email_data)
            else:
                print(f"SubscriptionView.create: No CEO email found for tenant {tenant_id}, skipping email notification")

            serializer = SubscriptionSerializer(subscription)
            print(f"SubscriptionView.create: Subscription created for tenant_id={tenant_id}, plan_id={plan_id}")
            return Response({
                'data': 'Subscription created successfully.',
                'subscription': serializer.data,
                'carried_days': result.get('carried_days', 0),
                'previous_subscription_id': result.get('previous_subscription_id')
            }, status=status.HTTP_201_CREATED)

        except ValidationError as e:
            print(f"SubscriptionView.create: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"SubscriptionView.create: Unexpected error - {str(e)}")
            return Response({'error': 'Subscription creation failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='activate-trial', permission_classes=[IsAuthenticated])
    @swagger_helper("Subscriptions", "activate_trial_subscription")
    def activate_trial(self, request):
        """
        Activate a 7-day free trial for a tenant/user before purchasing.
        Body params (JSON):
        - plan_id (optional): UUID of plan to use for the trial. If omitted, the endpoint will pick a default active plan for the tenant's industry.
        """
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            tenant_id = serializer.validated_data['tenant_id']
            plan_id = serializer.validated_data.get('plan_id')

            # If no plan_id provided, select a default plan
            if not plan_id:
                industry = None
                try:
                    client = IdentityServiceClient(request=request)
                    tenant_info = client.get_tenant(tenant_id=str(tenant_id))
                    industry = tenant_info.get('industry') if tenant_info else None
                    print(f"SubscriptionView.activate_trial: Tenant industry - {industry}")
                except Exception as e:
                    print(f"SubscriptionView.activate_trial: Error fetching tenant info - {str(e)}")
                    industry = None

                plans_qs = Plan.objects.filter(is_active=True, discontinued=False)
                if industry:
                    plans_qs = plans_qs.filter(industry__iexact=industry)
                plan = plans_qs.first()
                if not plan:
                    print("SubscriptionView.activate_trial: No available plan found")
                    return Response({'error': 'No available plan found to attach to trial'},
                                    status=status.HTTP_400_BAD_REQUEST)
                plan_id = str(plan.id)

            machine_number = serializer.validated_data.get('machine_number')
            
            # Trial doesn't need a plan_id - it gives access with limits (100 users, 10 branches)
            subscription, result = subscription_service.create_subscription(
                tenant_id=str(tenant_id),
                plan_id=None,  # Trial is plan-agnostic - no plan required
                user=str(request.user.email),
                machine_number=machine_number,
                is_trial=True
            )

            # Send trial activation confirmation email
            email_data = {
                'user_email': request.user.email,
                'email_type': 'confirmation',
                'subject': 'Free Trial Activated',
                'message': f'Your 7-day free trial has been activated successfully! You now have access to up to 100 users and 10 branches. Your trial ends on {subscription.trial_end_date.strftime("%Y-%m-%d") if subscription.trial_end_date else "N/A"}.',
                'action': 'Trial Activated'
            }
            send_email_via_service(email_data)

            serializer = SubscriptionSerializer(subscription, context={'request': request})
            print(f"SubscriptionView.activate_trial: Trial activated for tenant_id={tenant_id}, machine_number={machine_number}")
            return Response({
                'data': 'Trial activated successfully',
                'subscription': serializer.data,
                'machine_number': machine_number
            }, status=status.HTTP_201_CREATED)

        except ValidationError as e:
            print(f"SubscriptionView.activate_trial: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"SubscriptionView.activate_trial: Unexpected error - {str(e)}")
            return Response({'error': 'Failed to activate trial', 'details': str(e)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


    @action(detail=True, methods=['post'], url_path='suspend')
    @swagger_helper("Subscriptions", "suspend_subscription_action")
    def suspend_subscription(self, request, pk=None):
        try:
            serializer = self.get_serializer(data={**request.data, 'subscription_id': pk})
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.suspend_subscription(
                subscription_id=pk,
                user=str(request.user.id),
                reason=serializer.validated_data['reason']
            )

            # Send suspension notification email
            email_data = {
                'user_email': request.user.email,
                'email_type': 'general',
                'subject': 'Subscription Suspended',
                'message': f'Your subscription has been suspended. Reason: {serializer.validated_data["reason"]}',
                'action': 'Subscription Suspended'
            }
            send_email_via_service(email_data)

            serializer = SubscriptionSerializer(subscription)
            print(
                f"SubscriptionView.suspend_subscription: Subscription suspended for id={pk}, reason={serializer.validated_data['reason']}")
            return Response({
                'data': 'Subscription suspended successfully.',
                'subscription': serializer.data
            })

        except ValidationError as e:
            print(f"SubscriptionView.suspend_subscription: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"SubscriptionView.suspend_subscription: Unexpected error - {str(e)}")
            return Response({'error': 'Subscription suspension failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)





    @action(detail=False, methods=['post'], url_path='check-expired')
    @swagger_helper("Subscriptions", "check_expired_subscriptions_action")
    def check_expired_subscriptions(self, request):
        try:
            role = getattr(self.request, 'role', None)
            if not (self.request.user.is_superuser or (role and role.lower() == 'superuser')):
                print("SubscriptionView.check_expired_subscriptions: Permission denied")
                return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)

            subscription_service = SubscriptionService(request)
            result = subscription_service.check_expired_subscriptions()
            print(f"SubscriptionView.check_expired_subscriptions: Checked expired subscriptions, result={result}")

            return Response(result)

        except Exception as e:
            print(f"SubscriptionView.check_expired_subscriptions: Unexpected error - {str(e)}")
            return Response({'error': 'Expired subscription check failed'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'], url_path='audit-logs')
    @swagger_helper("Subscriptions", "get_subscription_audit_logs")
    def get_audit_logs(self, request, pk=None):
        try:
            subscription = self.get_object()
            audit_logs = subscription.audit_logs.all()[:50]
            serializer = self.get_serializer(audit_logs, many=True)
            print(
                f"SubscriptionView.get_audit_logs: Retrieved {len(serializer.data)} audit logs for subscription_id={pk}")
            return Response({
                'subscription_id': str(subscription.id),
                'audit_logs': serializer.data,
                'count': len(serializer.data)
            })

        except Exception as e:
            print(f"SubscriptionView.get_audit_logs: Unexpected error - {str(e)}")
            return Response({'error': 'Failed to retrieve audit logs'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)




    @swagger_helper("Subscriptions", "list_subscriptions")
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @swagger_helper("Subscriptions", "retrieve_subscription")
    def retrieve(self, request, *args, **kwargs):
        try:
            print(f"SubscriptionView.retrieve: Retrieving subscription id={kwargs.get('pk')}")
            return super().retrieve(request, *args, **kwargs)
        except Exception as e:
            print(f"SubscriptionView.retrieve: Unexpected error - {str(e)}")
            return Response({'error': 'Failed to retrieve subscription'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Subscriptions", "partial_update_subscription")
    def partial_update(self, request, *args, **kwargs):
        try:
            print(f"SubscriptionView.partial_update: Partially updating subscription id={kwargs.get('pk')}")
            return super().partial_update(request, *args, **kwargs)
        except Exception as e:
            print(f"SubscriptionView.partial_update: Unexpected error - {str(e)}")
            return Response({'error': 'Subscription partial update failed'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Subscriptions", "delete_subscription")
    def destroy(self, request, *args, **kwargs):
        try:
            print(f"SubscriptionView.destroy: Deleting subscription id={kwargs.get('pk')}")
            return super().destroy(request, *args, **kwargs)
        except Exception as e:
            print(f"SubscriptionView.destroy: Unexpected error - {str(e)}")
            return Response({'error': 'Subscription deletion failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
