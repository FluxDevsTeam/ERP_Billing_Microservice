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

from .models import Plan, Subscription, AuditLog
from apps.payment.models import Payment
from .serializers import PlanSerializer, SubscriptionSerializer, PaymentSerializer, SubscriptionCreateSerializer, PlanChangeSerializer, AdvanceRenewalSerializer, AutoRenewToggleSerializer
from .permissions import IsSuperuser, IsCEO, IsCEOorSuperuser, CanViewEditSubscription, PlanReadOnlyForCEO
from .utils import IdentityServiceClient, _extract_user_role, swagger_helper
from .services import SubscriptionService, UsageMonitorService, PaymentRetryService
from .validators import SubscriptionValidator, UsageValidator, InputValidator
from .circuit_breaker import CircuitBreakerManager

logger = logging.getLogger('billing')


class PlanView(viewsets.ModelViewSet):
    serializer_class = PlanSerializer
    permission_classes = [IsAuthenticated, PlanReadOnlyForCEO]
    filter_backends = [DjangoFilterBackend, SearchFilter]
    search_fields = ['name']
    filterset_fields = ['id', 'name', 'price', 'industry', 'is_active', 'discontinued']

    def get_queryset(self):
        user = self.request.user
        base_qs = Plan.objects.all()
        user_role = _extract_user_role(user)

        if user.is_superuser or user_role == 'superuser':
            return base_qs

        if user_role == 'ceo':
            tenant_id = getattr(user, 'tenant', None)
            if not tenant_id:
                return Plan.objects.none()

            circuit_breaker_manager = CircuitBreakerManager()
            identity_breaker = circuit_breaker_manager.get_breaker('identity_service')

            if not identity_breaker.can_execute():
                cache_key = f"tenant_industry_{tenant_id}"
                industry = cache.get(cache_key, 'Other')
                return base_qs.filter(industry=industry, is_active=True)

            try:
                client = IdentityServiceClient(request=self.request)
                tenant = client.get_tenant(tenant_id=tenant_id)
                industry = tenant.get('industry', 'Other') if tenant and isinstance(tenant, dict) else 'Other'
                cache_key = f"tenant_industry_{tenant_id}"
                cache.set(cache_key, industry, 300)
                return base_qs.filter(industry=industry, is_active=True)
            except Exception as e:
                logger.error(f"Plan filtering failed for tenant {tenant_id}: {str(e)}")
                identity_breaker.record_failure()
                cache_key = f"tenant_industry_{tenant_id}"
                industry = cache.get(cache_key, 'Other')
                return base_qs.filter(industry=industry, is_active=True)

        return Plan.objects.none()

    @swagger_helper("Subscription Management", "create")
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

            if errors:
                logger.warning(f"Plan creation failed: {errors}")
                return Response({'errors': errors}, status=status.HTTP_400_BAD_REQUEST)

            return super().create(request, *args, **kwargs)

        except Exception as e:
            logger.error(f"Plan creation failed: {str(e)}")
            return Response({'error': 'Plan creation failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Access Management", "health_check")
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
            logger.error(f"Plan health check failed: {str(e)}")
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

    @swagger_helper("Plan", "destroy")
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


class SubscriptionView(viewsets.ModelViewSet):
    queryset = Subscription.objects.all()
    serializer_class = SubscriptionSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter]
    search_fields = ['tenant_id']
    filterset_fields = ['tenant_id', 'plan', 'status', 'auto_renew']

    def get_queryset(self):
        user = self.request.user
        user_role = _extract_user_role(user)
        if user.is_superuser or user_role == 'superuser':
            return Subscription.objects.select_related('plan', 'scheduled_plan').all()
        tenant_id = getattr(user, 'tenant', None)
        if tenant_id and user_role == 'ceo':
            try:
                tenant_id = uuid.UUID(tenant_id)
                return Subscription.objects.select_related('plan', 'scheduled_plan').filter(tenant_id=tenant_id)
            except ValueError:
                return Subscription.objects.none()
        return Subscription.objects.none()

    def get_permissions(self):
        if self.action in ['list', 'create']:
            return [IsAuthenticated(), IsCEOorSuperuser()]
        if self.action in ['retrieve', 'update', 'partial_update', 'renew_subscription', 'change_plan', 'advance_renewal', 'toggle_auto_renew']:
            return [IsAuthenticated(), CanViewEditSubscription()]
        if self.action == 'destroy':
            return [IsAuthenticated(), IsSuperuser()]
        return [IsAuthenticated()]

    @swagger_helper("Subscription Management", "create")
    def create(self, request, *args, **kwargs):
        try:
            serializer = SubscriptionCreateSerializer(data=request.data, context={'request': request})
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            tenant_id = serializer.validated_data['tenant_id']
            plan_id = serializer.validated_data['plan_id']

            subscription, result = subscription_service.create_subscription(
                tenant_id=str(tenant_id),
                plan_id=str(plan_id),
                user=str(request.user.id)
            )

            logger.info(f"Subscription created: {subscription.id} for tenant {tenant_id}")
            serializer = self.get_serializer(subscription)
            return Response({
                'data': 'Subscription created successfully.',
                'subscription': serializer.data
            }, status=status.HTTP_201_CREATED)

        except ValidationError as e:
            logger.warning(f"Subscription creation failed: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Subscription creation failed: {str(e)}")
            return Response({'error': 'Subscription creation failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def list(self, request, *args, **kwargs):
        try:
            cache_key = f"subscriptions_{request.user.id}_{request.GET.urlencode()}"
            cached_result = cache.get(cache_key)
            if cached_result:
                return Response(cached_result)

            queryset = self.get_queryset()
            serializer = self.get_serializer(queryset, many=True)
            result = {
                'count': queryset.count(),
                'results': serializer.data
            }

            cache.set(cache_key, result, 300)
            return Response(result)

        except Exception as e:
            logger.error(f"Subscription listing failed: {str(e)}")
            return Response({'error': 'Failed to retrieve subscriptions'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='renew')
    @swagger_helper("Subscriptions", "renew_subscription")
    def renew_subscription(self, request, pk=None):
        try:
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.renew_subscription(
                subscription_id=pk,
                user=str(request.user.id)
            )

            logger.info(f"Subscription renewed: {pk}")
            serializer = self.get_serializer(subscription)
            return Response({
                'data': 'Subscription renewed successfully.',
                'subscription': serializer.data
            })

        except ValidationError as e:
            logger.warning(f"Subscription renewal failed for {pk}: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Subscription renewal failed for {pk}: {str(e)}")
            return Response({'error': 'Subscription renewal failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='suspend')
    @swagger_helper("Subscriptions", "suspend_subscription")
    def suspend_subscription(self, request, pk=None):
        try:
            reason = request.data.get('reason', 'Administrative suspension')
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.suspend_subscription(
                subscription_id=pk,
                user=str(request.user.id),
                reason=reason
            )

            logger.info(f"Subscription suspended: {pk}")
            serializer = self.get_serializer(subscription)
            return Response({
                'data': 'Subscription suspended successfully.',
                'subscription': serializer.data
            })

        except ValidationError as e:
            logger.warning(f"Subscription suspension failed for {pk}: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Subscription suspension failed for {pk}: {str(e)}")
            return Response({'error': 'Subscription suspension failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='change-plan')
    @swagger_helper("Subscriptions", "change_plan")
    def change_plan(self, request, pk=None):
        try:
            serializer = PlanChangeSerializer(data=request.data, context={'subscription': self.get_object()})
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.change_plan(
                subscription_id=pk,
                new_plan_id=serializer.validated_data['new_plan_id'],
                user=str(request.user.id),
                immediate=serializer.validated_data['immediate']
            )

            logger.info(f"Plan changed for subscription {pk} to {result['new_plan']}")
            serializer = self.get_serializer(subscription)
            return Response({
                'data': 'Plan changed successfully.',
                'subscription': serializer.data,
                'old_plan': result.get('old_plan'),
                'new_plan': result.get('new_plan'),
                'immediate': result.get('immediate')
            })

        except ValidationError as e:
            logger.warning(f"Plan change failed for {pk}: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Plan change failed for {pk}: {str(e)}")
            return Response({'error': 'Plan change failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='advance-renewal')
    @swagger_helper("Subscriptions", "advance_renewal")
    def advance_renewal(self, request, pk=None):
        try:
            serializer = AdvanceRenewalSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.renew_in_advance(
                subscription_id=pk,
                periods=serializer.validated_data['periods'],
                plan_id=serializer.validated_data.get('plan_id'),
                user=str(request.user.id)
            )

            logger.info(f"Advance renewal for subscription {pk}: {result['periods']} periods")
            serializer = self.get_serializer(subscription)
            return Response({
                'data': 'Subscription renewed in advance successfully.',
                'subscription': serializer.data,
                'periods': result['periods'],
                'amount': result['amount']
            })

        except ValidationError as e:
            logger.warning(f"Advance renewal failed for {pk}: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Advance renewal failed for {pk}: {str(e)}")
            return Response({'error': 'Advance renewal failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='toggle-auto-renew')
    @swagger_helper("Subscriptions", "toggle_auto_renew")
    def toggle_auto_renew(self, request, pk=None):
        try:
            serializer = AutoRenewToggleSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.toggle_auto_renew(
                subscription_id=pk,
                auto_renew=serializer.validated_data['auto_renew'],
                user=str(request.user.id)
            )

            logger.info(f"Auto-renew toggled for subscription {pk}: {result['auto_renew']}")
            serializer = self.get_serializer(subscription)
            return Response({
                'data': 'Auto-renew status updated successfully.',
                'subscription': serializer.data,
                'auto_renew': result['auto_renew']
            })

        except ValidationError as e:
            logger.warning(f"Auto-renew toggle failed for {pk}: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Auto-renew toggle failed for {pk}: {str(e)}")
            return Response({'error': 'Auto-renew toggle failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='check-expired')
    @swagger_helper("Subscriptions", "check_expired_subscriptions")
    def check_expired_subscriptions(self, request):
        try:
            if not (self.request.user.is_superuser or _extract_user_role(self.request.user) == 'superuser'):
                logger.warning(f"Expired subscription check denied for user {self.request.user.id}")
                return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)

            subscription_service = SubscriptionService(request)
            result = subscription_service.check_expired_subscriptions()

            logger.info(f"Expired subscription check completed: {result['processed_count']} processed")
            return Response(result)

        except Exception as e:
            logger.error(f"Expired subscription check failed: {str(e)}")
            return Response({'error': 'Expired subscription check failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'], url_path='audit-logs')
    @swagger_helper("Subscriptions", "get_audit_logs")
    def get_audit_logs(self, request, pk=None):
        try:
            subscription = self.get_object()
            audit_logs = subscription.audit_logs.all()[:50]

            logs_data = []
            for log in audit_logs:
                logs_data.append({
                    'id': str(log.id),
                    'action': log.action,
                    'user': log.user,
                    'timestamp': log.timestamp.isoformat(),
                    'details': log.details,
                    'ip_address': log.ip_address
                })

            logger.info(f"Audit logs retrieved for subscription {pk}")
            return Response({
                'subscription_id': str(subscription.id),
                'audit_logs': logs_data,
                'count': len(logs_data)
            })

        except Exception as e:
            logger.error(f"Audit logs retrieval failed for {pk}: {str(e)}")
            return Response({'error': 'Failed to retrieve audit logs'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CustomerPortalViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, CanViewEditSubscription]

    @action(detail=False, methods=['get'], url_path='details')
    def get_subscription_details(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                logger.warning(f"Subscription details request failed: No tenant for user {request.user.id}")
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:
                logger.warning(f"Subscription details request failed: No subscription for tenant {tenant_id}")
                return Response({'error': 'No subscription found'}, status=status.HTTP_404_NOT_FOUND)

            serializer = SubscriptionSerializer(subscription)
            logger.info(f"Subscription details retrieved for tenant {tenant_id}")
            return Response({
                'subscription': serializer.data,
                'payment_history': PaymentSerializer(subscription.payments.all(), many=True).data
            })

        except Exception as e:
            logger.error(f"Subscription details retrieval failed: {str(e)}")
            return Response({'error': 'Failed to retrieve subscription details'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='change-plan')
    def change_plan(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                logger.warning(f"Plan change request failed: No tenant for user {request.user.id}")
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:
                logger.warning(f"Plan change request failed: No subscription for tenant {tenant_id}")
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

            logger.info(f"Plan changed for tenant {tenant_id} to {result['new_plan']}")
            return Response({
                'data': 'Plan changed successfully.',
                'subscription': SubscriptionSerializer(subscription).data,
                'old_plan': result.get('old_plan'),
                'new_plan': result.get('new_plan'),
                'immediate': result.get('immediate'),
                'prorated_amount': result.get('prorated_amount')
            })

        except ValidationError as e:
            logger.warning(f"Plan change failed for tenant {tenant_id}: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Plan change failed for tenant {tenant_id}: {str(e)}")
            return Response({'error': 'Plan change failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='advance-renewal')
    def advance_renewal(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                logger.warning(f"Advance renewal request failed: No tenant for user {request.user.id}")
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:
                logger.warning(f"Advance renewal request failed: No subscription for tenant {tenant_id}")
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

            logger.info(f"Advance renewal for tenant {tenant_id}: {result['periods']} periods")
            return Response({
                'data': 'Subscription renewed in advance successfully.',
                'subscription': SubscriptionSerializer(subscription).data,
                'periods': result['periods'],
                'amount': result['amount']
            })

        except ValidationError as e:
            logger.warning(f"Advance renewal failed for tenant {tenant_id}: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Advance renewal failed for tenant {tenant_id}: {str(e)}")
            return Response({'error': 'Advance renewal failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='toggle-auto-renew')
    def toggle_auto_renew(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                logger.warning(f"Auto-renew toggle request failed: No tenant for user {request.user.id}")
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:
                logger.warning(f"Auto-renew toggle request failed: No subscription for tenant {tenant_id}")
                return Response({'error': 'No subscription found'}, status=status.HTTP_404_NOT_FOUND)

            serializer = AutoRenewToggleSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.toggle_auto_renew(
                subscription_id=str(subscription.id),
                auto_renew=serializer.validated_data['auto_renew'],
                user=str(request.user.id)
            )

            logger.info(f"Auto-renew toggled for tenant {tenant_id}: {result['auto_renew']}")
            return Response({
                'data': 'Auto-renew status updated successfully.',
                'subscription': SubscriptionSerializer(subscription).data,
                'auto_renew': result['auto_renew']
            })

        except ValidationError as e:
            logger.warning(f"Auto-renew toggle failed for tenant {tenant_id}: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Auto-renew toggle failed for tenant {tenant_id}: {str(e)}")
            return Response({'error': 'Auto-renew toggle failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AccessCheckView(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def list(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                logger.warning(f"Access check failed: No tenant for user {request.user.id}")
                return Response({
                    "access": False,
                    "message": "No tenant associated with user.",
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_403_FORBIDDEN)

            try:
                tenant_id = uuid.UUID(tenant_id)
            except ValueError:
                logger.warning(f"Access check failed: Invalid tenant ID {tenant_id}")
                return Response({
                    "access": False,
                    "message": "Invalid tenant ID format.",
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_400_BAD_REQUEST)

            cache_key = f"access_check_{tenant_id}"
            cached_result = cache.get(cache_key)
            if cached_result:
                return Response(cached_result)

            try:
                subscription = Subscription.objects.select_related('plan').get(tenant_id=tenant_id)
            except Subscription.DoesNotExist:
                logger.warning(f"Access check failed: No subscription for tenant {tenant_id}")
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

            cache.set(cache_key, response_data, 60)
            logger.info(f"Access check for tenant {tenant_id}: {message}")
            return Response(response_data, status=status.HTTP_200_OK if access else status.HTTP_403_FORBIDDEN)

        except Exception as e:
            logger.error(f"Access check failed: {str(e)}")
            return Response({
                "access": False,
                "message": "Access check failed",
                "error": str(e),
                "timestamp": timezone.now().isoformat()
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'], url_path='limits')
    def check_limits(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                logger.warning(f"Usage limits check failed: No tenant for user {request.user.id}")
                return Response({
                    "error": "No tenant associated with user.",
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_403_FORBIDDEN)

            try:
                tenant_id = uuid.UUID(tenant_id)
            except ValueError:
                logger.warning(f"Usage limits check failed: Invalid tenant ID {tenant_id}")
                return Response({
                    "error": "Invalid tenant ID format.",
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_400_BAD_REQUEST)

            cache_key = f"usage_limits_{tenant_id}"
            cached_result = cache.get(cache_key)
            if cached_result:
                return Response(cached_result)

            try:
                subscription = Subscription.objects.select_related('plan').get(tenant_id=tenant_id)
            except Subscription.DoesNotExist:
                logger.warning(f"Usage limits check failed: No subscription for tenant {tenant_id}")
                return Response({
                    "error": "No subscription found for tenant.",
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_403_FORBIDDEN)

            if subscription.status not in ['active', 'trial']:
                logger.warning(f"Usage limits check failed: Subscription {subscription.status} for tenant {tenant_id}")
                return Response({
                    "error": f"Subscription {subscription.status}.",
                    "subscription_status": subscription.status,
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_403_FORBIDDEN)

            usage_monitor = UsageMonitorService(request)
            usage_data = usage_monitor.check_usage_limits(str(tenant_id))

            if usage_data['status'] == 'error':
                logger.error(f"Usage limits check failed: {usage_data['message']}")
                return Response({
                    "error": usage_data['message'],
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            subscription_info = usage_monitor.get_subscription_info(str(tenant_id))

            response_data = {
                "tenant_id": str(tenant_id),
                "subscription_id": str(subscription.id),
                "plan": {
                    "id": str(subscription.plan.id),
                    "name": subscription.plan.name,
                    "max_users": subscription.plan.max_users,
                    "max_branches": subscription.plan.max_branches
                },
                "usage": usage_data.get('usage', {}),
                "overall_blocked": usage_data.get('overall_blocked', False),
                "subscription_info": subscription_info,
                "subscription_status": subscription.status,
                "remaining_days": subscription.get_remaining_days(),
                "timestamp": timezone.now().isoformat()
            }

            cache.set(cache_key, response_data, 120)
            logger.info(f"Usage limits checked for tenant {tenant_id}")
            return Response(response_data)

        except Exception as e:
            logger.error(f"Usage limits check failed: {str(e)}")
            return Response({
                "error": "Usage limits check failed",
                "message": str(e),
                "timestamp": timezone.now().isoformat()
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Access Management", "health_check")
    @action(detail=False, methods=['get'], url_path='health')
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

            logger.info(f"Access health check: {health_data['status']}")
            return Response(health_data, status=200 if overall_healthy else 503)

        except Exception as e:
            logger.error(f"Access health check failed: {str(e)}")
            return Response({
                'status': 'unhealthy',
                'error': str(e),
                'timestamp': timezone.now().isoformat()
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        @action(detail=False, methods=['post'], url_path='validate-usage')
        def validate_usage(self, request):
            try:
                tenant_id = getattr(request.user, 'tenant', None)
                if not tenant_id:
                    logger.warning(f"Usage validation failed: No tenant for user {request.user.id}")
                    return Response({
                        "error": "No tenant associated with user."
                    }, status=status.HTTP_403_FORBIDDEN)

                operation = request.data.get('operation')
                if not operation:
                    logger.warning(f"Usage validation failed: Operation type missing")
                    return Response({
                        "error": "Operation type is required."
                    }, status=status.HTTP_400_BAD_REQUEST)

                try:
                    tenant_id = uuid.UUID(tenant_id)
                    subscription = Subscription.objects.select_related('plan').get(tenant_id=tenant_id)
                except (ValueError, Subscription.DoesNotExist):
                    logger.warning(f"Usage validation failed: Invalid tenant or no subscription for {tenant_id}")
                    return Response({
                        "error": "Invalid tenant or no subscription found."
                    }, status=status.HTTP_400_BAD_REQUEST)

                if subscription.status not in ['active', 'trial']:
                    logger.warning(f"Usage validation failed: Subscription {subscription.status} for tenant {tenant_id}")
                    return Response({
                        "allowed": False,
                        "reason": f"Subscription is {subscription.status}"
                    }, status=status.HTTP_403_FORBIDDEN)

                usage_validator = UsageValidator(request)
                usage_data = usage_validator.validate_usage_limits(str(tenant_id), subscription.plan)

                if not usage_data['valid']:
                    logger.warning(f"Usage validation failed for tenant {tenant_id}: {usage_data['errors']}")
                    return Response({
                        "allowed": False,
                        "reason": "Usage limits exceeded",
                        "errors": usage_data['errors'],
                        "usage": usage_data['usage']
                    }, status=status.HTTP_403_FORBIDDEN)

                # Handle specific operations
                if operation == 'create_user':
                    current_users = usage_data['usage'].get('current_users', 0)
                    if current_users >= subscription.plan.max_users:
                        logger.warning(f"Usage validation failed for tenant {tenant_id}: User limit exceeded")
                        return Response({
                            "allowed": False,
                            "reason": f"User limit ({subscription.plan.max_users}) exceeded",
                            "usage": usage_data['usage']
                        }, status=status.HTTP_403_FORBIDDEN)
                
                logger.info(f"Usage validation passed for tenant {tenant_id}, operation {operation}")
                return Response({
                    "allowed": True,
                    "usage": usage_data['usage'],
                    "plan_limits": {
                        "max_users": subscription.plan.max_users,
                        "max_branches": subscription.plan.max_branches
                    }
                })

            except Exception as e:
                logger.error(f"Usage validation failed: {str(e)}")
                return Response({
                    "error": "Usage validation failed",
                    "message": str(e),
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)