# apps/billing/views.py
from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.filters import SearchFilter
from rest_framework.decorators import action
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.response import Response
from django.db import transaction
from django.core.cache import cache
from django.utils import timezone
from django.core.exceptions import ValidationError
import uuid
import logging

logger = logging.getLogger(__name__)

from .models import Plan, Subscription, AuditLog, AutoRenewal
from apps.payment.models import Payment
from .serializers import (
    PlanSerializer, SubscriptionSerializer, PaymentSerializer,
    SubscriptionCreateSerializer, TrialActivationSerializer,
    SubscriptionRenewSerializer, SubscriptionSuspendSerializer,
    PlanChangeSerializer, AdvanceRenewalSerializer, AutoRenewToggleSerializer, AuditLogSerializer,
    AutoRenewalSerializer, AutoRenewalCreateSerializer, AutoRenewalUpdateSerializer
)
from .permissions import IsSuperuser, IsCEO, IsCEOorSuperuser, CanViewEditSubscription, PlanReadOnlyForCEO
from .utils import IdentityServiceClient, swagger_helper
from .services import SubscriptionService, UsageMonitorService, PaymentRetryService, AutoRenewalService
from .validators import SubscriptionValidator, UsageValidator, InputValidator
from .circuit_breaker import CircuitBreakerManager
from api.email_service import send_email_via_service


class PlanView(viewsets.ModelViewSet):
    serializer_class = PlanSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter]
    search_fields = ['name']
    filterset_fields = ['id', 'name', 'price', 'industry', 'is_active', 'discontinued']

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            try:
                print("PlanView.get_permissions - user:", getattr(self.request, 'user', None))
                print("PlanView.get_permissions - possible role fields:", getattr(self.request, 'role', None),
                      getattr(self.request.user, 'role', None))
            except Exception:
                pass
            return [IsAuthenticated(), IsCEOorSuperuser()]
        return [IsAuthenticated(), IsSuperuser()]

    def get_queryset(self):
        user = self.request.user
        base_qs = Plan.objects.all()
        role = getattr(user, 'role', None)

        print(f"PlanView.get_queryset - User: {user}, Role: {role}")

        if user.is_superuser or (role and role.lower() == 'superuser'):
            print("User is superuser - showing all plans")
            return base_qs

        if role and role.lower() == 'ceo':
            print("User is CEO")
            tenant_id = getattr(user, 'tenant', None)
            if not tenant_id:
                print("No tenant ID found for CEO")
                return Plan.objects.none()
            try:
                client = IdentityServiceClient(request=self.request)
                tenant = client.get_tenant(tenant_id=tenant_id)
                industry = tenant.get('industry') if tenant and isinstance(tenant, dict) else None
                print(f"CEO tenant industry: {industry}")
                if not industry:
                    print("No industry found for CEO's tenant")
                    return Plan.objects.none()
                result = base_qs.filter(is_active=True, industry__iexact=industry, discontinued=False)
                print(f"Found {result.count()} plans for industry: {industry}")
                return result
            except Exception as e:
                print(f"Error getting tenant data: {str(e)}")
                return Plan.objects.none()

        print("User has no relevant role")
        return Plan.objects.none()

    @swagger_helper("Plan", "create")
    def create(self, request, *args, **kwargs):
        try:
            validator = InputValidator()
            errors = {}

            name = request.data.get('name')
            if not name:
                errors['name'] = ['Name is required']

            price = request.data.get('price')
            price_error = validator.validate_positive_number(price, 'Price')
            if price_error:
                errors['price'] = [price_error]

            industry = request.data.get('industry')
            if industry:
                industry_choices = [choice[0] for choice in Plan.INDUSTRY_CHOICES]
                industry_error = validator.validate_choice(industry, industry_choices, 'Industry')
                if industry_error:
                    errors['industry'] = [industry_error]

            billing_period = request.data.get('billing_period')
            if billing_period:
                period_choices = [choice[0] for choice in Plan.PERIOD_CHOICES]
                period_error = validator.validate_choice(billing_period, period_choices, 'Billing Period')
                if period_error:
                    errors['billing_period'] = [period_error]

            if errors:
                return Response({'errors': errors}, status=status.HTTP_400_BAD_REQUEST)

            return super().create(request, *args, **kwargs)

        except Exception as e:
            return Response({'error': 'Plan creation failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Plan", "health_check")
    @action(detail=False, methods=['get'], url_path='health')
    def health_check(self, request):
        try:
            total_plans = Plan.objects.count()
            active_plans = Plan.objects.filter(is_active=True).count()
            return Response({
                'status': 'healthy',
                'total_plans': total_plans,
                'active_plans': active_plans,
                'timestamp': timezone.now().isoformat()
            })
        except Exception as e:
            return Response({
                'status': 'unhealthy',
                'error': str(e),
                'timestamp': timezone.now().isoformat()
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    @swagger_helper("Plan", "list")
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @swagger_helper("Plan", "retrieve")
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @swagger_helper("Plan", "partial_update")
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @swagger_helper("Plan", "update")
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @swagger_helper("Plan", "destroy")
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


class SubscriptionView(viewsets.ModelViewSet):
    queryset = Subscription.objects.all()
    filter_backends = [DjangoFilterBackend, SearchFilter]
    search_fields = ['tenant_id']
    filterset_fields = ['tenant_id', 'plan', 'status', 'auto_renew']

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
        if self.action in ['create', 'update', 'partial_update', 'renew_subscription', 'change_plan', 'advance_renewal',
                           'extend_subscription', 'toggle_auto_renew', 'suspend_subscription', 'activate_trial']:
            return [IsAuthenticated(), CanViewEditSubscription()]
        if self.action == 'destroy':
            return [IsAuthenticated(), IsSuperuser()]
        return [IsAuthenticated()]

    def get_serializer_class(self):
        if self.action == 'create':
            return SubscriptionCreateSerializer
        if self.action == 'activate_trial':
            return TrialActivationSerializer
        if self.action == 'renew_subscription':
            return SubscriptionRenewSerializer
        if self.action == 'suspend_subscription':
            return SubscriptionSuspendSerializer
        if self.action == 'change_plan':
            return PlanChangeSerializer
        if self.action == 'advance_renewal':
            return AdvanceRenewalSerializer
        if self.action == 'toggle_auto_renew':
            return AutoRenewToggleSerializer
        if self.action == 'get_audit_logs':
            return AuditLogSerializer
        return SubscriptionSerializer

    @swagger_helper("Subscriptions", "create")
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

            # Create auto-renewal if auto_renew is True (default)
            auto_renew = serializer.validated_data.get('auto_renew', True)
            if auto_renew:
                try:
                    auto_renewal_service = AutoRenewalService(request)
                    auto_renewal_service.create_auto_renewal(
                        tenant_id=str(tenant_id),
                        plan_id=str(plan_id),
                        expiry_date=subscription.end_date,
                        user_id=str(request.user.id),
                        subscription_id=str(subscription.id)
                    )
                except Exception as e:
                    logger.warning(f"Failed to create auto-renewal for subscription {subscription.id}: {str(e)}")

            # Send welcome email
            email_data = {
                'user_email': request.user.email,
                'email_type': 'confirmation',
                'subject': 'Welcome to Your New Subscription',
                'message': f'Your subscription to {subscription.plan.name} plan has been created successfully.',
                'action': 'Subscription Created'
            }
            send_email_via_service(email_data)

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
    @swagger_helper("Subscriptions", "create")
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
            
            subscription, result = subscription_service.create_subscription(
                tenant_id=str(tenant_id),
                plan_id=str(plan_id),
                user=str(request.user.email),
                machine_number=machine_number,
                is_trial=True
            )

            serializer = SubscriptionSerializer(subscription, context={'request': request})
            print(f"SubscriptionView.activate_trial: Trial activated for tenant_id={tenant_id}, plan_id={plan_id}, machine_number={machine_number}")
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

    @action(detail=True, methods=['post'], url_path='renew')
    @swagger_helper("Subscriptions", "renew_subscription")
    def renew_subscription(self, request, pk=None):
        try:
            serializer = self.get_serializer(data={'subscription_id': pk})
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.renew_subscription(
                subscription_id=pk,
                user=str(request.user.id)
            )

            # Send renewal confirmation email
            email_data = {
                'user_email': request.user.email,
                'email_type': 'confirmation',
                'subject': 'Subscription Renewed',
                'message': f'Your subscription has been renewed successfully. Next renewal date: {subscription.end_date.strftime("%Y-%m-%d")}',
                'action': 'Subscription Renewed'
            }
            send_email_via_service(email_data)

            serializer = SubscriptionSerializer(subscription)
            print(f"SubscriptionView.renew_subscription: Subscription renewed for id={pk}")
            return Response({
                'data': 'Subscription renewed successfully.',
                'subscription': serializer.data
            })

        except ValidationError as e:
            print(f"SubscriptionView.renew_subscription: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"SubscriptionView.renew_subscription: Unexpected error - {str(e)}")
            return Response({'error': 'Subscription renewal failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='suspend')
    @swagger_helper("Subscriptions", "suspend_subscription")
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

    @action(detail=True, methods=['post'], url_path='change-plan')
    @swagger_helper("Subscriptions", "change_plan")
    def change_plan(self, request, pk=None):
        try:
            subscription = self.get_object()
            serializer = self.get_serializer(data=request.data, context={'subscription': subscription})
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.change_plan(
                subscription_id=pk,
                new_plan_id=serializer.validated_data['new_plan_id'],
                user=str(request.user.id),
                immediate=True  # Always immediate now, upgrades happen immediately, downgrades are scheduled
            )

            # Send plan change confirmation email
            change_message = f'Your plan has been changed from {result["old_plan"]} to {result["new_plan"]}.'
            if result.get('is_upgrade'):
                change_message += f' Remaining value: {result.get("remaining_value", 0):.2f}, Amount to pay: {result.get("prorated_amount", 0):.2f}'
            elif result.get('is_downgrade'):
                change_message += ' Downgrade scheduled for after current period ends.'
            
            email_data = {
                'user_email': request.user.email,
                'email_type': 'confirmation',
                'subject': 'Plan Change Confirmation',
                'message': change_message,
                'action': 'Plan Changed'
            }
            send_email_via_service(email_data)

            serializer = SubscriptionSerializer(subscription)
            print(
                f"SubscriptionView.change_plan: Plan changed for subscription_id={pk}, new_plan_id={serializer.validated_data['new_plan_id']}")
            return Response({
                'data': 'Plan changed successfully.',
                'subscription': serializer.data,
                'old_plan': result.get('old_plan'),
                'new_plan': result.get('new_plan'),
                'change_type': result.get('change_type'),
                'is_upgrade': result.get('is_upgrade'),
                'is_downgrade': result.get('is_downgrade'),
                'prorated_amount': result.get('prorated_amount'),
                'remaining_value': result.get('remaining_value'),
                'remaining_days': result.get('remaining_days'),
                'requires_payment': result.get('requires_payment'),
                'scheduled': result.get('scheduled', False)
            })

        except ValidationError as e:
            print(f"SubscriptionView.change_plan: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"SubscriptionView.change_plan: Unexpected error - {str(e)}")
            return Response({'error': 'Plan change failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='advance-renewal')
    @swagger_helper("Subscriptions", "advance_renewal")
    def advance_renewal(self, request, pk=None):
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.renew_in_advance(
                subscription_id=pk,
                periods=serializer.validated_data['periods'],
                plan_id=serializer.validated_data.get('plan_id'),
                user=str(request.user.id)
            )

            serializer = SubscriptionSerializer(subscription)
            print(
                f"SubscriptionView.advance_renewal: Advance renewal for subscription_id={pk}, periods={serializer.validated_data['periods']}")
            return Response({
                'data': 'Subscription renewed in advance successfully.',
                'subscription': serializer.data,
                'periods': result['periods'],
                'amount': result['amount']
            })

        except ValidationError as e:
            print(f"SubscriptionView.advance_renewal: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"SubscriptionView.advance_renewal: Unexpected error - {str(e)}")
            return Response({'error': 'Advance renewal failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='extend')
    @swagger_helper("Subscriptions", "extend_subscription")
    def extend_subscription(self, request, pk=None):
        """
        Manually extend subscription when remaining days is below 30.
        Adds one billing period to the existing subscription.
        """
        try:
            subscription = self.get_object()
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.extend_subscription(
                subscription_id=pk,
                user=str(request.user.id)
            )

            serializer = SubscriptionSerializer(subscription)
            return Response({
                'data': 'Subscription extended successfully.',
                'subscription': serializer.data,
                'new_end_date': result['new_end_date'],
                'remaining_days_before': result['remaining_days_before']
            })

        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Subscription extension failed: {str(e)}")
            return Response({'error': 'Subscription extension failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='toggle-auto-renew')
    @swagger_helper("Subscriptions", "toggle_auto_renew")
    def toggle_auto_renew(self, request, pk=None):
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            auto_renew = serializer.validated_data['auto_renew']
            
            subscription = self.get_object()
            auto_renewal_service = AutoRenewalService(request)
            
            # Find existing auto-renewal for this subscription
            auto_renewal = AutoRenewal.objects.filter(
                subscription_id=pk,
                status__in=['active', 'paused']
            ).first()
            
            if auto_renew and not auto_renewal:
                # Create auto-renewal
                auto_renewal, result = auto_renewal_service.create_auto_renewal(
                    tenant_id=str(subscription.tenant_id),
                    plan_id=str(subscription.plan.id),
                    expiry_date=subscription.end_date,
                    user_id=str(request.user.id),
                    subscription_id=pk
                )
                message = 'Auto-renew enabled successfully.'
            elif not auto_renew and auto_renewal:
                # Cancel auto-renewal
                auto_renewal, result = auto_renewal_service.cancel_auto_renewal(
                    auto_renewal_id=str(auto_renewal.id),
                    user_id=str(request.user.id)
                )
                message = 'Auto-renew disabled successfully.'
            elif auto_renew and auto_renewal and auto_renewal.status != 'active':
                # Reactivate auto-renewal
                auto_renewal, result = auto_renewal_service.update_auto_renewal(
                    auto_renewal_id=str(auto_renewal.id),
                    status='active',
                    user_id=str(request.user.id)
                )
                message = 'Auto-renew enabled successfully.'
            else:
                message = 'Auto-renew status is already set as requested.'

            serializer = SubscriptionSerializer(subscription)
            return Response({
                'data': message,
                'subscription': serializer.data,
                'auto_renew': auto_renew
            })

        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Auto-renew toggle failed: {str(e)}")
            return Response({'error': 'Auto-renew toggle failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='check-expired')
    @swagger_helper("Subscriptions", "check_expired_subscriptions")
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
    @swagger_helper("Subscriptions", "get_audit_logs")
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

    @action(detail=True, methods=['post'], url_path='change-card')
    @swagger_helper("Subscriptions", "change_subscription_card")
    def change_subscription_card(self, request, pk=None):
        """
        Change the payment card for auto-renewal subscription.
        
        Body params (JSON):
        - new_authorization_code: The authorization code from new payment (required for Paystack)
        - provider: Payment provider ('paystack' or 'flutterwave')
        """
        try:
            subscription = self.get_object()
            subscription_service = SubscriptionService(request)
            
            new_authorization_code = request.data.get('new_authorization_code')
            provider = request.data.get('provider', 'paystack')
            
            if not new_authorization_code:
                return Response(
                    {'error': 'new_authorization_code is required for card change'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if provider == 'flutterwave':
                return Response(
                    {
                        'error': 'Flutterwave does not support direct card changes',
                        'message': 'Please make a new payment with the desired card. It will automatically be used for future renewals.',
                        'action_required': 'new_payment'
                    }, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            result = subscription_service.change_subscription_card(
                subscription_id=pk,
                new_payment_token=new_authorization_code,
                user=str(request.user.id)
            )
            
            if result['status'] == 'success':
                # Send confirmation email
                email_data = {
                    'user_email': request.user.email,
                    'email_type': 'confirmation',
                    'subject': 'Payment Card Updated',
                    'message': f'Your payment card has been successfully updated for auto-renewal. New subscription code: {result.get("new_subscription_code", "N/A")}',
                    'action': 'Card Updated'
                }
                send_email_via_service(email_data)
                
                return Response({
                    'data': 'Payment card updated successfully for auto-renewal',
                    'subscription_id': str(subscription.id),
                    'new_subscription_code': result.get('new_subscription_code'),
                    'provider': provider
                })
            else:
                return Response(
                    {'error': result.get('message', 'Card change failed')}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
                
        except Exception as e:
            print(f"SubscriptionView.change_subscription_card: Unexpected error - {str(e)}")
            return Response({'error': 'Card change failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='manual-payment')
    @swagger_helper("Subscriptions", "manual_payment_with_saved_card")
    def manual_payment_with_saved_card(self, request, pk=None):
        """
        Process a manual payment using saved card details for early renewal or top-up.
        
        Body params (JSON):
        - amount: Amount to charge (optional, defaults to plan price)
        - reason: Reason for manual payment (optional)
        """
        try:
            subscription = self.get_object()
            subscription_service = SubscriptionService(request)
            
            amount = request.data.get('amount')
            reason = request.data.get('reason', 'manual_payment')
            
            if amount:
                try:
                    from decimal import Decimal
                    amount = Decimal(str(amount))
                    if amount <= 0:
                        return Response(
                            {'error': 'Amount must be greater than 0'}, 
                            status=status.HTTP_400_BAD_REQUEST
                        )
                except (ValueError, TypeError):
                    return Response(
                        {'error': 'Invalid amount format'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            result = subscription_service.manual_payment_with_saved_card(
                subscription_id=pk,
                amount=amount,
                user=str(request.user.id)
            )
            
            if result['status'] == 'success':
                return Response({
                    'data': 'Manual payment initiated successfully',
                    'subscription_id': str(subscription.id),
                    'payment_url': result.get('payment_url'),
                    'reference': result.get('reference'),
                    'amount': str(amount) if amount else str(subscription.plan.price),
                    'reason': reason,
                    'message': result.get('message', 'Please complete payment using your saved card')
                })
            else:
                error_msg = result.get('message', 'Manual payment failed')
                if result.get('status') == 'skipped':
                    return Response(
                        {'error': error_msg, 'action_required': result.get('action_required')}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                else:
                    return Response(
                        {'error': error_msg}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
        except Exception as e:
            print(f"SubscriptionView.manual_payment_with_saved_card: Unexpected error - {str(e)}")
            return Response({'error': 'Manual payment failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='manual-payment-new-card')
    @swagger_helper("Subscriptions", "manual_payment_with_new_card")
    def manual_payment_with_new_card(self, request, pk=None):
        """
        Process a manual payment using new card details for early renewal or top-up.
        This is different from saved card payments - user provides new payment details.
        
        Body params (JSON):
        - amount: Amount to charge (optional, defaults to plan price)
        - reason: Reason for manual payment (optional)
        - provider: Payment provider ('paystack' or 'flutterwave')
        """
        try:
            subscription = self.get_object()
            
            amount = request.data.get('amount')
            reason = request.data.get('reason', 'manual_payment_new_card')
            provider = request.data.get('provider', 'paystack')
            
            if amount:
                try:
                    from decimal import Decimal
                    amount = Decimal(str(amount))
                    if amount <= 0:
                        return Response(
                            {'error': 'Amount must be greater than 0'}, 
                            status=status.HTTP_400_BAD_REQUEST
                        )
                except (ValueError, TypeError):
                    return Response(
                        {'error': 'Invalid amount format'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                from decimal import Decimal
                amount = Decimal(str(subscription.plan.price))
            
            # Get tenant information
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                return Response(
                    {'error': 'No tenant associated with user'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Create payment record for tracking
            payment = Payment.objects.create(
                plan=subscription.plan,
                subscription=subscription,
                amount=amount,
                transaction_id=str(uuid.uuid4()),
                status='pending',
                provider=provider,
                payment_type='manual'
            )
            
            # Initialize payment based on provider
            if provider == 'paystack':
                from .payments import initiate_paystack_payment
                
                # Generate confirm token
                from .utils import generate_confirm_token
                confirm_token = generate_confirm_token(request.user, str(subscription.plan.id))
                
                # Initialize Paystack payment
                response = initiate_paystack_payment(
                    confirm_token=confirm_token,
                    amount=float(amount),
                    user=request.user,
                    plan_id=str(subscription.plan.id),
                    tenant_id=str(tenant_id),
                    tenant_name=getattr(request.user, 'tenant_name', None)
                )
                
            elif provider == 'flutterwave':
                from .payments import initiate_flutterwave_payment
                
                # Generate confirm token
                from .utils import generate_confirm_token
                confirm_token = generate_confirm_token(request.user, str(subscription.plan.id))
                
                # Initialize Flutterwave payment
                response = initiate_flutterwave_payment(
                    confirm_token=confirm_token,
                    amount=float(amount),
                    user=request.user,
                    plan_id=str(subscription.plan.id),
                    tenant_id=str(tenant_id),
                    tenant_name=getattr(request.user, 'tenant_name', None)
                )
            else:
                payment.delete()  # Clean up payment record
                return Response(
                    {'error': f'Unsupported payment provider: {provider}'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if response.status_code == 200:
                response_data = response.data
                return Response({
                    'data': 'Manual payment initiated successfully with new card',
                    'payment_id': str(payment.id),
                    'subscription_id': str(subscription.id),
                    'payment_url': response_data.get('payment_link'),
                    'authorization_url': response_data.get('authorization_url'),
                    'reference': response_data.get('tx_ref'),
                    'amount': str(amount),
                    'provider': provider,
                    'reason': reason,
                    'message': 'Please complete payment with your new card details'
                })
            else:
                # Clean up failed payment record
                payment.delete()
                error_data = response.data if hasattr(response, 'data') else {'error': 'Payment initialization failed'}
                return Response(error_data, status=response.status_code)
                
        except Exception as e:
            print(f"SubscriptionView.manual_payment_with_new_card: Unexpected error - {str(e)}")
            return Response({'error': 'Manual payment with new card failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'], url_path='payment-info')
    @swagger_helper("Subscriptions", "get_payment_provider_info")
    def get_payment_provider_info(self, request, pk=None):
        """
        Get information about the payment provider setup for this subscription.
        """
        try:
            subscription = self.get_object()
            subscription_service = SubscriptionService(request)
            
            # Get auto-renewal record
            auto_renewal = AutoRenewal.objects.filter(
                subscription=subscription,
                status='active'
            ).first()
            
            if not auto_renewal:
                return Response({
                    'subscription_id': str(subscription.id),
                    'auto_renewal': False,
                    'payment_provider': None,
                    'message': 'No active auto-renewal found'
                })
            
            # Extract payment provider information
            provider_info = subscription_service._extract_payment_provider_info(auto_renewal)
            
            # Get last payment information
            last_payment = subscription.payments.filter(status='completed').order_by('-payment_date').first()
            
            payment_details = None
            if last_payment:
                payment_details = {
                    'provider': last_payment.provider,
                    'amount': str(last_payment.amount),
                    'date': last_payment.payment_date.isoformat(),
                    'transaction_id': last_payment.transaction_id
                }
            
            return Response({
                'subscription_id': str(subscription.id),
                'auto_renewal': True,
                'payment_provider': provider_info.get('provider'),
                'provider_details': provider_info,
                'last_payment': payment_details,
                'auto_renewal_status': auto_renewal.status,
                'next_renewal_date': auto_renewal.next_renewal_date.isoformat() if auto_renewal.next_renewal_date else None
            })
            
        except Exception as e:
            print(f"SubscriptionView.get_payment_provider_info: Unexpected error - {str(e)}")
            return Response({'error': 'Failed to retrieve payment info'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Subscriptions", "list")
    def list(self, request, *args, **kwargs):
        try:
            queryset = self.get_queryset()
            serializer = self.get_serializer(queryset, many=True)
            result = {
                'count': queryset.count(),
                'results': serializer.data
            }
            print(f"SubscriptionView.list: Listed {queryset.count()} subscriptions")
            return Response(result)

        except Exception as e:
            print(f"SubscriptionView.list: Unexpected error - {str(e)}")
            return Response({'error': 'Failed to retrieve subscriptions'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Subscriptions", "retrieve")
    def retrieve(self, request, *args, **kwargs):
        try:
            print(f"SubscriptionView.retrieve: Retrieving subscription id={kwargs.get('pk')}")
            return super().retrieve(request, *args, **kwargs)
        except Exception as e:
            print(f"SubscriptionView.retrieve: Unexpected error - {str(e)}")
            return Response({'error': 'Failed to retrieve subscription'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Subscriptions", "partial_update")
    def partial_update(self, request, *args, **kwargs):
        try:
            print(f"SubscriptionView.partial_update: Partially updating subscription id={kwargs.get('pk')}")
            return super().partial_update(request, *args, **kwargs)
        except Exception as e:
            print(f"SubscriptionView.partial_update: Unexpected error - {str(e)}")
            return Response({'error': 'Subscription partial update failed'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Subscriptions", "update")
    def update(self, request, *args, **kwargs):
        try:
            print(f"SubscriptionView.update: Updating subscription id={kwargs.get('pk')}")
            return super().update(request, *args, **kwargs)
        except Exception as e:
            print(f"SubscriptionView.update: Unexpected error - {str(e)}")
            return Response({'error': 'Subscription update failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Subscriptions", "destroy")
    def destroy(self, request, *args, **kwargs):
        try:
            print(f"SubscriptionView.destroy: Deleting subscription id={kwargs.get('pk')}")
            return super().destroy(request, *args, **kwargs)
        except Exception as e:
            print(f"SubscriptionView.destroy: Unexpected error - {str(e)}")
            return Response({'error': 'Subscription deletion failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CustomerPortalViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, CanViewEditSubscription]

    @action(detail=False, methods=['get'], url_path='details')
    @swagger_helper("Customer Portal", "get_subscription_details")
    def get_subscription_details(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:
                return Response({'error': 'No subscription found'}, status=status.HTTP_404_NOT_FOUND)

            serializer = SubscriptionSerializer(subscription)
            return Response({
                'subscription': serializer.data,
                'payment_history': PaymentSerializer(subscription.payments.all(), many=True).data
            })

        except Exception as e:
            return Response({'error': 'Failed to retrieve subscription details'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='change-plan')
    @swagger_helper("Customer Portal", "change_plan")
    def change_plan(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                print("CustomerPortalViewSet.change_plan: No tenant associated with user")
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:
                print("CustomerPortalViewSet.change_plan: No subscription found")
                return Response({'error': 'No subscription found'}, status=status.HTTP_404_NOT_FOUND)

            serializer = PlanChangeSerializer(data=request.data, context={'subscription': subscription})
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.change_plan(
                subscription_id=str(subscription.id),
                new_plan_id=serializer.validated_data['new_plan_id'],
                user=str(request.user.id),
                immediate=True
            )

            print(
                f"CustomerPortalViewSet.change_plan: Plan changed for subscription_id={subscription.id}, new_plan_id={serializer.validated_data['new_plan_id']}")
            return Response({
                'data': 'Plan changed successfully.',
                'subscription': SubscriptionSerializer(subscription).data,
                'old_plan': result.get('old_plan'),
                'new_plan': result.get('new_plan'),
                'change_type': result.get('change_type'),
                'is_upgrade': result.get('is_upgrade'),
                'is_downgrade': result.get('is_downgrade'),
                'prorated_amount': result.get('prorated_amount'),
                'remaining_value': result.get('remaining_value'),
                'remaining_days': result.get('remaining_days'),
                'requires_payment': result.get('requires_payment'),
                'scheduled': result.get('scheduled', False)
            })

        except ValidationError as e:
            print(f"CustomerPortalViewSet.change_plan: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"CustomerPortalViewSet.change_plan: Unexpected error - {str(e)}")
            return Response({'error': 'Plan change failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='advance-renewal')
    @swagger_helper("Customer Portal", "advance_renewal")
    def advance_renewal(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                print("CustomerPortalViewSet.advance_renewal: No tenant associated with user")
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:
                print("CustomerPortalViewSet.advance_renewal: No subscription found")
                return Response({'error': 'No subscription found'}, status=status.HTTP_404_NOT_FOUND)

            serializer = AdvanceRenewalSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.renew_in_advance(
                subscription_id=str(subscription.id),
                periods=serializer.validated_data['periods'],
                plan_id=serializer.validated_data.get('plan_id'),
                user=str(request.user.id)
            )

            print(
                f"CustomerPortalViewSet.advance_renewal: Advance renewal for subscription_id={subscription.id}, periods={serializer.validated_data['periods']}")
            return Response({
                'data': 'Subscription renewed in advance successfully.',
                'subscription': SubscriptionSerializer(subscription).data,
                'periods': result['periods'],
                'amount': result['amount']
            })

        except ValidationError as e:
            print(f"CustomerPortalViewSet.advance_renewal: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"CustomerPortalViewSet.advance_renewal: Unexpected error - {str(e)}")
            return Response({'error': 'Advance renewal failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='toggle-auto-renew')
    @swagger_helper("Customer Portal", "toggle_auto_renew")
    def toggle_auto_renew(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                print("CustomerPortalViewSet.toggle_auto_renew: No tenant associated with user")
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:
                print("CustomerPortalViewSet.toggle_auto_renew: No subscription found")
                return Response({'error': 'No subscription found'}, status=status.HTTP_404_NOT_FOUND)

            serializer = AutoRenewToggleSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            auto_renew = serializer.validated_data['auto_renew']
            
            auto_renewal_service = AutoRenewalService(request)
            
            # Find existing auto-renewal for this subscription
            auto_renewal = AutoRenewal.objects.filter(
                subscription_id=subscription.id,
                status__in=['active', 'paused', 'canceled']
            ).first()
            
            if auto_renew and not auto_renewal:
                # Create auto-renewal
                auto_renewal, result = auto_renewal_service.create_auto_renewal(
                    tenant_id=str(tenant_id),
                    plan_id=str(subscription.plan.id),
                    expiry_date=subscription.end_date,
                    user_id=str(request.user.id),
                    subscription_id=str(subscription.id)
                )
                message = 'Auto-renew enabled successfully.'
            elif not auto_renew and auto_renewal:
                # Cancel auto-renewal
                auto_renewal, result = auto_renewal_service.cancel_auto_renewal(
                    auto_renewal_id=str(auto_renewal.id),
                    user_id=str(request.user.id)
                )
                message = 'Auto-renew disabled successfully.'
            elif auto_renew and auto_renewal and auto_renewal.status != 'active':
                # Reactivate auto-renewal
                auto_renewal, result = auto_renewal_service.update_auto_renewal(
                    auto_renewal_id=str(auto_renewal.id),
                    status='active',
                    user_id=str(request.user.id)
                )
                message = 'Auto-renew enabled successfully.'
            else:
                message = 'Auto-renew status is already set as requested.'

            print(
                f"CustomerPortalViewSet.toggle_auto_renew: Auto-renew toggled to {auto_renew} for subscription_id={subscription.id}")
            return Response({
                'data': message,
                'subscription': SubscriptionSerializer(subscription).data,
                'auto_renew': auto_renew
            })

        except ValidationError as e:
            print(f"CustomerPortalViewSet.toggle_auto_renew: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"CustomerPortalViewSet.toggle_auto_renew: Unexpected error - {str(e)}")
            logger.error(f"Auto-renew toggle failed: {str(e)}")
            return Response({'error': 'Auto-renew toggle failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='extend')
    @swagger_helper("Customer Portal", "extend_subscription")
    def extend_subscription(self, request):
        """Manually extend subscription when remaining days is below 30"""
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:
                return Response({'error': 'No subscription found'}, status=status.HTTP_404_NOT_FOUND)

            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.extend_subscription(
                subscription_id=str(subscription.id),
                user=str(request.user.id)
            )

            return Response({
                'data': 'Subscription extended successfully.',
                'subscription': SubscriptionSerializer(subscription).data,
                'new_end_date': result['new_end_date'],
                'remaining_days_before': result['remaining_days_before']
            })

        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Subscription extension failed: {str(e)}")
            return Response({'error': 'Subscription extension failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='manual-payment')
    @swagger_helper("Customer Portal", "manual_payment_with_saved_card")
    def manual_payment_with_saved_card(self, request):
        """Manual payment using saved card (customer portal)"""
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:
                return Response({'error': 'No subscription found'}, status=status.HTTP_404_NOT_FOUND)

            subscription_service = SubscriptionService(request)
            amount = request.data.get('amount')
            reason = request.data.get('reason', 'manual_payment')
            
            if amount:
                try:
                    from decimal import Decimal
                    amount = Decimal(str(amount))
                    if amount <= 0:
                        return Response(
                            {'error': 'Amount must be greater than 0'}, 
                            status=status.HTTP_400_BAD_REQUEST
                        )
                except (ValueError, TypeError):
                    return Response(
                        {'error': 'Invalid amount format'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            result = subscription_service.manual_payment_with_saved_card(
                subscription_id=str(subscription.id),
                amount=amount,
                user=str(request.user.id)
            )
            
            if result['status'] == 'success':
                return Response({
                    'data': 'Manual payment initiated successfully',
                    'subscription_id': str(subscription.id),
                    'payment_url': result.get('payment_url'),
                    'reference': result.get('reference'),
                    'amount': str(amount) if amount else str(subscription.plan.price),
                    'reason': reason,
                    'message': result.get('message', 'Please complete payment using your saved card')
                })
            else:
                error_msg = result.get('message', 'Manual payment failed')
                if result.get('status') == 'skipped':
                    return Response(
                        {'error': error_msg, 'action_required': result.get('action_required')}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                else:
                    return Response(
                        {'error': error_msg}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
        except Exception as e:
            print(f"CustomerPortalViewSet.manual_payment_with_saved_card: Unexpected error - {str(e)}")
            return Response({'error': 'Manual payment failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='manual-payment-new-card')
    @swagger_helper("Customer Portal", "manual_payment_with_new_card")
    def manual_payment_with_new_card(self, request):
        """Manual payment using new card details (customer portal)"""
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:
                return Response({'error': 'No subscription found'}, status=status.HTTP_404_NOT_FOUND)
            
            subscription_service = SubscriptionService(request)
            amount = request.data.get('amount')
            reason = request.data.get('reason', 'manual_payment_new_card')
            provider = request.data.get('provider', 'paystack')
            
            if amount:
                try:
                    from decimal import Decimal
                    amount = Decimal(str(amount))
                    if amount <= 0:
                        return Response(
                            {'error': 'Amount must be greater than 0'}, 
                            status=status.HTTP_400_BAD_REQUEST
                        )
                except (ValueError, TypeError):
                    return Response(
                        {'error': 'Invalid amount format'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                from decimal import Decimal
                amount = Decimal(str(subscription.plan.price))
            
            # Create payment record for tracking
            payment = Payment.objects.create(
                plan=subscription.plan,
                subscription=subscription,
                amount=amount,
                transaction_id=str(uuid.uuid4()),
                status='pending',
                provider=provider,
                payment_type='manual'
            )
            
            # Initialize payment based on provider
            if provider == 'paystack':
                from .payments import initiate_paystack_payment
                
                # Generate confirm token
                from .utils import generate_confirm_token
                confirm_token = generate_confirm_token(request.user, str(subscription.plan.id))
                
                # Initialize Paystack payment
                response = initiate_paystack_payment(
                    confirm_token=confirm_token,
                    amount=float(amount),
                    user=request.user,
                    plan_id=str(subscription.plan.id),
                    tenant_id=str(tenant_id),
                    tenant_name=getattr(request.user, 'tenant_name', None)
                )
                
            elif provider == 'flutterwave':
                from .payments import initiate_flutterwave_payment
                
                # Generate confirm token
                from .utils import generate_confirm_token
                confirm_token = generate_confirm_token(request.user, str(subscription.plan.id))
                
                # Initialize Flutterwave payment
                response = initiate_flutterwave_payment(
                    confirm_token=confirm_token,
                    amount=float(amount),
                    user=request.user,
                    plan_id=str(subscription.plan.id),
                    tenant_id=str(tenant_id),
                    tenant_name=getattr(request.user, 'tenant_name', None)
                )
            else:
                payment.delete()  # Clean up payment record
                return Response(
                    {'error': f'Unsupported payment provider: {provider}'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if response.status_code == 200:
                response_data = response.data
                return Response({
                    'data': 'Manual payment initiated successfully with new card',
                    'payment_id': str(payment.id),
                    'subscription_id': str(subscription.id),
                    'payment_url': response_data.get('payment_link'),
                    'authorization_url': response_data.get('authorization_url'),
                    'reference': response_data.get('tx_ref'),
                    'amount': str(amount),
                    'provider': provider,
                    'reason': reason,
                    'message': 'Please complete payment with your new card details'
                })
            else:
                # Clean up failed payment record
                payment.delete()
                error_data = response.data if hasattr(response, 'data') else {'error': 'Payment initialization failed'}
                return Response(error_data, status=response.status_code)
            
        except Exception as e:
            print(f"CustomerPortalViewSet.manual_payment_with_new_card: Unexpected error - {str(e)}")
            return Response({'error': 'Manual payment with new card failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AutoRenewalViewSet(viewsets.ModelViewSet):
    """ViewSet for managing auto-renewals"""
    queryset = AutoRenewal.objects.all()
    serializer_class = AutoRenewalSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter]
    search_fields = ['tenant_id', 'user_id']
    filterset_fields = ['tenant_id', 'plan', 'status']

    def get_queryset(self):
        user = self.request.user
        role = getattr(user, 'role', None)
        
        if user.is_superuser or (role and role.lower() == 'superuser'):
            return AutoRenewal.objects.select_related('plan', 'subscription').all()
        
        tenant_id = getattr(user, 'tenant', None)
        if tenant_id and role and role.lower() == 'ceo':
            try:
                tenant_id = uuid.UUID(str(tenant_id))
                return AutoRenewal.objects.select_related('plan', 'subscription').filter(tenant_id=tenant_id)
            except ValueError:
                return AutoRenewal.objects.none()
        
        return AutoRenewal.objects.none()

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [IsAuthenticated(), IsCEOorSuperuser()]
        return [IsAuthenticated(), CanViewEditSubscription()]

    def get_serializer_class(self):
        if self.action == 'create':
            return AutoRenewalCreateSerializer
        if self.action in ['update', 'partial_update']:
            return AutoRenewalUpdateSerializer
        return AutoRenewalSerializer

    @swagger_helper("AutoRenewal", "create")
    def create(self, request, *args, **kwargs):
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            auto_renewal_service = AutoRenewalService(request)
            
            auto_renewal, result = auto_renewal_service.create_auto_renewal(
                tenant_id=str(serializer.validated_data['tenant_id']),
                plan_id=str(serializer.validated_data['plan_id']),
                expiry_date=serializer.validated_data['expiry_date'],
                user_id=str(request.user.id),
                subscription_id=str(serializer.validated_data.get('subscription_id')) if serializer.validated_data.get('subscription_id') else None
            )
            
            response_serializer = AutoRenewalSerializer(auto_renewal)
            return Response({
                'data': 'Auto-renewal created successfully.',
                'auto_renewal': response_serializer.data
            }, status=status.HTTP_201_CREATED)
            
        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Auto-renewal creation failed: {str(e)}")
            return Response({'error': 'Auto-renewal creation failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='process')
    @swagger_helper("AutoRenewal", "process")
    def process_renewal(self, request, pk=None):
        """Manually trigger processing of an auto-renewal"""
        try:
            auto_renewal_service = AutoRenewalService(request)
            result = auto_renewal_service.process_auto_renewal(auto_renewal_id=pk)
            
            return Response(result)
            
        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Auto-renewal processing failed: {str(e)}")
            return Response({'error': 'Auto-renewal processing failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='cancel')
    @swagger_helper("AutoRenewal", "cancel")
    def cancel_renewal(self, request, pk=None):
        """Cancel an auto-renewal"""
        try:
            auto_renewal_service = AutoRenewalService(request)
            auto_renewal, result = auto_renewal_service.cancel_auto_renewal(
                auto_renewal_id=pk,
                user_id=str(request.user.id)
            )
            
            serializer = AutoRenewalSerializer(auto_renewal)
            return Response({
                'data': 'Auto-renewal canceled successfully.',
                'auto_renewal': serializer.data
            })
            
        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Auto-renewal cancellation failed: {str(e)}")
            return Response({'error': 'Auto-renewal cancellation failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='process-due')
    @swagger_helper("AutoRenewal", "process_due")
    def process_due_renewals(self, request):
        """Process all due auto-renewals (admin only)"""
        try:
            role = getattr(request.user, 'role', None)
            if not (request.user.is_superuser or (role and role.lower() == 'superuser')):
                return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
            
            auto_renewal_service = AutoRenewalService(request)
            result = auto_renewal_service.process_due_auto_renewals()
            
            return Response(result)
            
        except Exception as e:
            logger.error(f"Processing due auto-renewals failed: {str(e)}")
            return Response({'error': 'Processing failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
                "auto_renew": subscription.auto_renew,  # Backwards compatibility
                "auto_renewal_active": subscription.auto_renewals.filter(status='active').exists(),
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
