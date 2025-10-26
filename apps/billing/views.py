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

from .models import Plan, Subscription, AuditLog
from apps.payment.models import Payment
from .serializers import (
    PlanSerializer, SubscriptionSerializer, PaymentSerializer,
    SubscriptionCreateSerializer, TrialActivationSerializer,
    SubscriptionRenewSerializer, SubscriptionSuspendSerializer,
    PlanChangeSerializer, AdvanceRenewalSerializer, AutoRenewToggleSerializer
)
from .permissions import IsSuperuser, IsCEO, IsCEOorSuperuser, CanViewEditSubscription, PlanReadOnlyForCEO
from .utils import IdentityServiceClient, swagger_helper
from .services import SubscriptionService, UsageMonitorService, PaymentRetryService
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
                result = base_qs.filter(is_active=True, industry__iexact=industry)
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
                           'toggle_auto_renew', 'suspend_subscription', 'activate_trial']:
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

            subscription, result = subscription_service.create_subscription(
                tenant_id=str(tenant_id),
                plan_id=str(plan_id),
                user=str(request.user.id)
            )

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
                'subscription': serializer.data
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

            subscription, result = subscription_service.create_subscription(
                tenant_id=str(tenant_id),
                plan_id=str(plan_id),
                user=str(request.user.email)
            )

            serializer = SubscriptionSerializer(subscription, context={'request': request})
            print(f"SubscriptionView.activate_trial: Trial activated for tenant_id={tenant_id}, plan_id={plan_id}")
            return Response({
                'data': 'Trial activated successfully',
                'subscription': serializer.data
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
                immediate=serializer.validated_data['immediate']
            )

            # Send plan change confirmation email
            email_data = {
                'user_email': request.user.email,
                'email_type': 'confirmation',
                'subject': 'Plan Change Confirmation',
                'message': f'Your plan has been changed from {result["old_plan"]} to {result["new_plan"]}.',
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
                'immediate': result.get('immediate')
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

    @action(detail=True, methods=['post'], url_path='toggle-auto-renew')
    @swagger_helper("Subscriptions", "toggle_auto_renew")
    def toggle_auto_renew(self, request, pk=None):
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.toggle_auto_renew(
                subscription_id=pk,
                auto_renew=serializer.validated_data['auto_renew'],
                user=str(request.user.id)
            )

            serializer = SubscriptionSerializer(subscription)
            print(
                f"SubscriptionView.toggle_auto_renew: Auto-renew toggled to {serializer.validated_data['auto_renew']} for subscription_id={pk}")
            return Response({
                'data': 'Auto-renew status updated successfully.',
                'subscription': serializer.data,
                'auto_renew': result['auto_renew']
            })

        except ValidationError as e:
            print(f"SubscriptionView.toggle_auto_renew: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"SubscriptionView.toggle_auto_renew: Unexpected error - {str(e)}")
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
                print("CustomerPortalViewSet.get_subscription_details: No tenant associated with user")
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:
                print("CustomerPortalViewSet.get_subscription_details: No subscription found")
                return Response({'error': 'No subscription found'}, status=status.HTTP_404_NOT_FOUND)

            serializer = SubscriptionSerializer(subscription)
            print(f"CustomerPortalViewSet.get_subscription_details: Retrieved subscription for tenant_id={tenant_id}")
            return Response({
                'subscription': serializer.data,
                'payment_history': PaymentSerializer(subscription.payments.all(), many=True).data
            })

        except Exception as e:
            print(f"CustomerPortalViewSet.get_subscription_details: Unexpected error - {str(e)}")
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
                immediate=serializer.validated_data['immediate']
            )

            print(
                f"CustomerPortalViewSet.change_plan: Plan changed for subscription_id={subscription.id}, new_plan_id={serializer.validated_data['new_plan_id']}")
            return Response({
                'data': 'Plan changed successfully.',
                'subscription': SubscriptionSerializer(subscription).data,
                'old_plan': result.get('old_plan'),
                'new_plan': result.get('new_plan'),
                'immediate': result.get('immediate'),
                'prorated_amount': result.get('prorated_amount')
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
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.toggle_auto_renew(
                subscription_id=str(subscription.id),
                auto_renew=serializer.validated_data['auto_renew'],
                user=str(request.user.id)
            )

            print(
                f"CustomerPortalViewSet.toggle_auto_renew: Auto-renew toggled to {serializer.validated_data['auto_renew']} for subscription_id={subscription.id}")
            return Response({
                'data': 'Auto-renew status updated successfully.',
                'subscription': SubscriptionSerializer(subscription).data,
                'auto_renew': result['auto_renew']
            })

        except ValidationError as e:
            print(f"CustomerPortalViewSet.toggle_auto_renew: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"CustomerPortalViewSet.toggle_auto_renew: Unexpected error - {str(e)}")
            return Response({'error': 'Auto-renew toggle failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
                "auto_renew": subscription.auto_renew,
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

    @action(detail=False, methods=['get'], url_path='health')
    @swagger_helper("Access Check", "health_check")
    def health_check(self, request):
        try:
            db_healthy = True
            try:
                Subscription.objects.count()
            except Exception:
                db_healthy = False

            circuit_breaker_manager = CircuitBreakerManager()
            breaker_states = circuit_breaker_manager.get_all_states()

            cache_healthy = True
            try:
                cache.set('health_check', 'test', 10)
                cache.get('health_check')
            except Exception:
                cache_healthy = False

            overall_healthy = db_healthy and cache_healthy

            health_data = {
                'status': 'healthy' if overall_healthy else 'unhealthy',
                'database': 'healthy' if db_healthy else 'unhealthy',
                'cache': 'healthy' if cache_healthy else 'unhealthy',
                'circuit_breakers': breaker_states,
                'timestamp': timezone.now().isoformat()
            }

            print(f"AccessCheckView.health_check: Health status - {health_data['status']}")
            return Response(health_data, status=200 if overall_healthy else 503)

        except Exception as e:
            print(f"AccessCheckView.health_check: Unexpected error - {str(e)}")
            return Response({
                'status': 'unhealthy',
                'error': str(e),
                'timestamp': timezone.now().isoformat()
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    @action(detail=False, methods=['post'], url_path='validate-usage')
    @swagger_helper("Access Check", "validate_usage")
    def validate_usage(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                print("AccessCheckView.validate_usage: No tenant associated with user")
                return Response({
                    "error": "No tenant associated with user."
                }, status=status.HTTP_403_FORBIDDEN)

            operation = request.data.get('operation')
            if not operation:
                print("AccessCheckView.validate_usage: Operation type is required")
                return Response({
                    "error": "Operation type is required."
                }, status=status.HTTP_400_BAD_REQUEST)

            try:
                tenant_id = uuid.UUID(str(tenant_id))
                subscription = Subscription.objects.select_related('plan').get(tenant_id=tenant_id)
            except (ValueError, Subscription.DoesNotExist):
                print("AccessCheckView.validate_usage: Invalid tenant or no subscription found")
                return Response({
                    "error": "Invalid tenant or no subscription found."
                }, status=status.HTTP_400_BAD_REQUEST)

            if subscription.status not in ['active', 'trial']:
                print(f"AccessCheckView.validate_usage: Subscription is {subscription.status}")
                return Response({
                    "allowed": False,
                    "reason": f"Subscription is {subscription.status}"
                }, status=status.HTTP_403_FORBIDDEN)

            usage_validator = UsageValidator(request)
            usage_data = usage_validator.validate_usage_limits(str(tenant_id), subscription.plan)

            if not usage_data['valid']:
                print(f"AccessCheckView.validate_usage: Usage limits exceeded - {usage_data['errors']}")
                return Response({
                    "allowed": False,
                    "reason": "Usage limits exceeded",
                    "errors": usage_data['errors'],
                    "usage": usage_data['usage']
                }, status=status.HTTP_403_FORBIDDEN)

            if operation == 'create_user':
                current_users = usage_data['usage'].get('current_users', 0)
                if current_users >= subscription.plan.max_users:
                    print(f"AccessCheckView.validate_usage: User limit ({subscription.plan.max_users}) exceeded")
                    return Response({
                        "allowed": False,
                        "reason": f"User limit ({subscription.plan.max_users}) exceeded",
                        "usage": usage_data['usage']
                    }, status=status.HTTP_403_FORBIDDEN)

            print(f"AccessCheckView.validate_usage: Usage validated for tenant_id={tenant_id}, operation={operation}")
            return Response({
                "allowed": True,
                "usage": usage_data['usage'],
                "plan_limits": {
                    "max_users": subscription.plan.max_users,
                    "max_branches": subscription.plan.max_branches
                }
            })

        except Exception as e:
            print(f"AccessCheckView.validate_usage: Unexpected error - {str(e)}")
            return Response({
                "error": "Usage validation failed",
                "message": str(e),
                "timestamp": timezone.now().isoformat()
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
