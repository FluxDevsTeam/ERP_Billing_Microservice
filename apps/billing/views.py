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
from .serializers import PlanSerializer, SubscriptionSerializer, PaymentSerializer, SubscriptionCreateSerializer, PlanChangeSerializer, AdvanceRenewalSerializer, AutoRenewToggleSerializer
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
            # Debug prints to inspect incoming user and role
            try:
                print("PlanView.get_permissions - user:", getattr(self.request, 'user', None))
                print("PlanView.get_permissions - possible role fields:", getattr(self.request, 'role', None), getattr(self.request.user, 'role', None), getattr(self.request.user, 'user_role', None), getattr(self.request.user, 'user_role_lowercase', None))
            except Exception:
                pass
            # Allow CEOs and superusers to view plans
            return [IsAuthenticated(), IsCEOorSuperuser()]
        # All other actions require superuser permissions
        return [IsAuthenticated(), IsSuperuser()]

    def get_queryset(self):
        user = self.request.user
        base_qs = Plan.objects.all()
        role = getattr(user, 'role', None)

        # Debug prints
        print(f"PlanView.get_queryset - User: {user}, Role: {role}")

        # Superuser sees all plans
        if user.is_superuser or (role and role.lower() == 'superuser'):
            print("User is superuser - showing all plans")
            return base_qs

        # CEO role handling
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
                
                # Only return active plans for the CEO's industry
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
    serializer_class = SubscriptionSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter]
    search_fields = ['tenant_id']
    filterset_fields = ['tenant_id', 'plan', 'status', 'auto_renew']

    def get_queryset(self):
        user = self.request.user
        role = getattr(user, 'role', None)
        if user.is_superuser or (role and role == 'superuser'):
            return Subscription.objects.select_related('plan', 'scheduled_plan').all()
        tenant_id = getattr(user, 'tenant', None)
        print(tenant_id)
        print(role)
        if tenant_id and role and role == 'ceo':
            try:
                tenant_id = uuid.UUID(tenant_id)
                return Subscription.objects.select_related('plan', 'scheduled_plan').filter(tenant_id=tenant_id)
            except ValueError:
                return Subscription.objects.none()
        return Subscription.objects.none()

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [IsAuthenticated(), IsCEOorSuperuser()]
        if self.action in ['create', 'update', 'partial_update', 'renew_subscription', 'change_plan', 'advance_renewal', 'toggle_auto_renew']:
            return [IsAuthenticated(), CanViewEditSubscription()]
        if self.action == 'destroy':
            return [IsAuthenticated(), IsSuperuser()]
        return [IsAuthenticated()]

    @swagger_helper("Subscriptions", "create")
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

            # Send welcome email
            email_data = {
                'user_email': request.user.email,
                'email_type': 'confirmation',
                'subject': 'Welcome to Your New Subscription',
                'message': f'Your subscription to {subscription.plan.name} plan has been created successfully.',
                'action': 'Subscription Created'
            }
            send_email_via_service(email_data)


            serializer = self.get_serializer(subscription)
            return Response({
                'data': 'Subscription created successfully.',
                'subscription': serializer.data
            }, status=status.HTTP_201_CREATED)

        except ValidationError as e:

            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:

            return Response({'error': 'Subscription creation failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='activate-trial', permission_classes=[IsAuthenticated])
    @swagger_helper("Subscriptions", "create")
    def activate_trial(self, request):
        """
        Activate a 7-day free trial for a tenant/user before purchasing.

        Body params (JSON):
        - tenant_id (optional): UUID of tenant. If omitted, will try to use tenant from request.user.
        - plan_id (optional): UUID of plan to use for the trial. If omitted, the endpoint will pick a default active plan for the tenant's industry.
        """
        try:
            user = request.user

            # Determine tenant_id: prefer explicit input, else try to read from user attribute
            tenant_id = request.data.get('tenant_id') or getattr(user, 'tenant', None)
            if not tenant_id:
                return Response({'error': 'tenant_id is required or must be available on the authenticated user'}, status=status.HTTP_400_BAD_REQUEST)

            # Determine plan: optional
            plan_id = request.data.get('plan_id')
            # If plan_id not provided, try to select a sensible default: first active plan for tenant industry
            if not plan_id:
                industry = None
                try:
                    client = IdentityServiceClient(request=request)
                    tenant_info = client.get_tenant(tenant_id=tenant_id)
                    industry = tenant_info.get('industry') if tenant_info else None
                except Exception:
                    industry = None

                plans_qs = Plan.objects.filter(is_active=True, discontinued=False)
                if industry:
                    plans_qs = plans_qs.filter(industry__iexact=industry)

                plan = plans_qs.first()
                if not plan:
                    return Response({'error': 'No available plan found to attach to trial'}, status=status.HTTP_400_BAD_REQUEST)
                plan_id = str(plan.id)

            # Use SubscriptionService to create the subscription (it enforces trial rules internally)
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.create_subscription(tenant_id=str(tenant_id), plan_id=str(plan_id), user=getattr(user, 'email', None))

            serializer = SubscriptionSerializer(subscription, context={'request': request})
            return Response({'data': 'Trial activated successfully', 'subscription': serializer.data}, status=status.HTTP_201_CREATED)

        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': 'Failed to activate trial', 'details': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Subscriptions", "list")
    def list(self, request, *args, **kwargs):
        try:
            queryset = self.get_queryset()
            serializer = self.get_serializer(queryset, many=True)
            result = {
                'count': queryset.count(),
                'results': serializer.data
            }
            return Response(result)

        except Exception as e:
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

            # Send renewal confirmation email
            email_data = {
                'user_email': request.user.email,
                'email_type': 'confirmation',
                'subject': 'Subscription Renewed',
                'message': f'Your subscription has been renewed successfully. Next renewal date: {subscription.end_date.strftime("%Y-%m-%d")}',
                'action': 'Subscription Renewed'
            }
            send_email_via_service(email_data)


            serializer = self.get_serializer(subscription)
            return Response({
                'data': 'Subscription renewed successfully.',
                'subscription': serializer.data
            })

        except ValidationError as e:

            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:

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

            # Send suspension notification email
            email_data = {
                'user_email': request.user.email,
                'email_type': 'general',
                'subject': 'Subscription Suspended',
                'message': f'Your subscription has been suspended. Reason: {reason}',
                'action': 'Subscription Suspended'
            }
            send_email_via_service(email_data)


            serializer = self.get_serializer(subscription)
            return Response({
                'data': 'Subscription suspended successfully.',
                'subscription': serializer.data
            })

        except ValidationError as e:

            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:

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

            # Send plan change confirmation email
            email_data = {
                'user_email': request.user.email,
                'email_type': 'confirmation',
                'subject': 'Plan Change Confirmation',
                'message': f'Your plan has been changed from {result["old_plan"]} to {result["new_plan"]}.',
                'action': 'Plan Changed'
            }
            send_email_via_service(email_data)


            serializer = self.get_serializer(subscription)
            return Response({
                'data': 'Plan changed successfully.',
                'subscription': serializer.data,
                'old_plan': result.get('old_plan'),
                'new_plan': result.get('new_plan'),
                'immediate': result.get('immediate')
            })

        except ValidationError as e:

            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:

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


            serializer = self.get_serializer(subscription)
            return Response({
                'data': 'Subscription renewed in advance successfully.',
                'subscription': serializer.data,
                'periods': result['periods'],
                'amount': result['amount']
            })

        except ValidationError as e:

            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:

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


            serializer = self.get_serializer(subscription)
            return Response({
                'data': 'Auto-renew status updated successfully.',
                'subscription': serializer.data,
                'auto_renew': result['auto_renew']
            })

        except ValidationError as e:

            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:

            return Response({'error': 'Auto-renew toggle failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='check-expired')
    @swagger_helper("Subscriptions", "check_expired_subscriptions")
    def check_expired_subscriptions(self, request):
        try:
            role = getattr(self.request, 'role', None)
            if not (self.request.user.is_superuser or (role and role == 'superuser')):

                return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)

            subscription_service = SubscriptionService(request)
            result = subscription_service.check_expired_subscriptions()


            return Response(result)

        except Exception as e:

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


            return Response({
                'subscription_id': str(subscription.id),
                'audit_logs': logs_data,
                'count': len(logs_data)
            })

        except Exception as e:

            return Response({'error': 'Failed to retrieve audit logs'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Subscriptions", "retrieve")
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @swagger_helper("Subscriptions", "partial_update")
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @swagger_helper("Subscriptions", "update")
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @swagger_helper("Subscriptions", "destroy")
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


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

            return Response({'error': 'Failed to retrieve subscription details'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='change-plan')
    @swagger_helper("Customer Portal", "change_plan")
    def change_plan(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:

                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:

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


            return Response({
                'data': 'Plan changed successfully.',
                'subscription': SubscriptionSerializer(subscription).data,
                'old_plan': result.get('old_plan'),
                'new_plan': result.get('new_plan'),
                'immediate': result.get('immediate'),
                'prorated_amount': result.get('prorated_amount')
            })

        except ValidationError as e:

            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:

            return Response({'error': 'Plan change failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='advance-renewal')
    @swagger_helper("Customer Portal", "advance_renewal")
    def advance_renewal(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:

                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:

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


            return Response({
                'data': 'Subscription renewed in advance successfully.',
                'subscription': SubscriptionSerializer(subscription).data,
                'periods': result['periods'],
                'amount': result['amount']
            })

        except ValidationError as e:

            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:

            return Response({'error': 'Advance renewal failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='toggle-auto-renew')
    @swagger_helper("Customer Portal", "toggle_auto_renew")
    def toggle_auto_renew(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:

                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:

                return Response({'error': 'No subscription found'}, status=status.HTTP_404_NOT_FOUND)

            serializer = AutoRenewToggleSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            subscription_service = SubscriptionService(request)
            subscription, result = subscription_service.toggle_auto_renew(
                subscription_id=str(subscription.id),
                auto_renew=serializer.validated_data['auto_renew'],
                user=str(request.user.id)
            )


            return Response({
                'data': 'Auto-renew status updated successfully.',
                'subscription': SubscriptionSerializer(subscription).data,
                'auto_renew': result['auto_renew']
            })

        except ValidationError as e:

            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:

            return Response({'error': 'Auto-renew toggle failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AccessCheckView(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    @swagger_helper("Access Check", "list")
    def list(self, request):
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:

                return Response({
                    "access": False,
                    "message": "No tenant associated with user.",
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_403_FORBIDDEN)

            try:
                tenant_id = uuid.UUID(tenant_id)
            except ValueError:

                return Response({
                    "access": False,
                    "message": "Invalid tenant ID format.",
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_400_BAD_REQUEST)



            try:
                subscription = Subscription.objects.select_related('plan').get(tenant_id=tenant_id)
            except Subscription.DoesNotExist:

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



            return Response(response_data, status=status.HTTP_200_OK if access else status.HTTP_403_FORBIDDEN)

        except Exception as e:

            return Response({
                "access": False,
                "message": "Access check failed",
                "error": str(e),
                "timestamp": timezone.now().isoformat()
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # @action(detail=False, methods=['get'], url_path='limits')
    # @swagger_helper("Access Check", "check_limits")
    # def check_limits(self, request):
    #     try:
    #         tenant_id = getattr(request.user, 'tenant', None)
    #         if not tenant_id:
    #
    #             return Response({
    #                 "error": "No tenant associated with user.",
    #                 "timestamp": timezone.now().isoformat()
    #             }, status=status.HTTP_403_FORBIDDEN)
    #
    #         try:
    #             tenant_id = uuid.UUID(tenant_id)
    #         except ValueError:
    #
    #             return Response({
    #                 "error": "Invalid tenant ID format.",
    #                 "timestamp": timezone.now().isoformat()
    #             }, status=status.HTTP_400_BAD_REQUEST)
    #
    #
    #
    #         try:
    #             subscription = Subscription.objects.select_related('plan').get(tenant_id=tenant_id)
    #         except Subscription.DoesNotExist:
    #
    #             return Response({
    #                 "error": "No subscription found for tenant.",
    #                 "timestamp": timezone.now().isoformat()
    #             }, status=status.HTTP_403_FORBIDDEN)
    #
    #         if subscription.status not in ['active', 'trial']:
    #
    #             return Response({
    #                 "error": f"Subscription {subscription.status}.",
    #                 "subscription_status": subscription.status,
    #                 "timestamp": timezone.now().isoformat()
    #             }, status=status.HTTP_403_FORBIDDEN)
    #
    #         usage_monitor = UsageMonitorService(request)
    #         usage_data = usage_monitor.check_usage_limits(str(tenant_id))
    #
    #         if usage_data['status'] == 'error':
    #
    #             return Response({
    #                 "error": usage_data['message'],
    #                 "timestamp": timezone.now().isoformat()
    #             }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    #
    #         subscription_info = usage_monitor.get_subscription_info(str(tenant_id))
    #
    #         response_data = {
    #             "tenant_id": str(tenant_id),
    #             "subscription_id": str(subscription.id),
    #             "plan": {
    #                 "id": str(subscription.plan.id),
    #                 "name": subscription.plan.name,
    #                 "max_users": subscription.plan.max_users,
    #                 "max_branches": subscription.plan.max_branches
    #             },
    #             "usage": usage_data.get('usage', {}),
    #             "overall_blocked": usage_data.get('overall_blocked', False),
    #             "subscription_info": subscription_info,
    #             "subscription_status": subscription.status,
    #             "remaining_days": subscription.get_remaining_days(),
    #             "timestamp": timezone.now().isoformat()
    #         }
    #
    #
    #
    #         return Response(response_data)
    #
    #     except Exception as e:
    #
    #         return Response({
    #             "error": "Usage limits check failed",
    #             "message": str(e),
    #             "timestamp": timezone.now().isoformat()
    #         }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Access Check", "health_check")
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


            return Response(health_data, status=200 if overall_healthy else 503)

        except Exception as e:

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

                    return Response({
                        "error": "No tenant associated with user."
                    }, status=status.HTTP_403_FORBIDDEN)

                operation = request.data.get('operation')
                if not operation:

                    return Response({
                        "error": "Operation type is required."
                    }, status=status.HTTP_400_BAD_REQUEST)

                try:
                    tenant_id = uuid.UUID(tenant_id)
                    subscription = Subscription.objects.select_related('plan').get(tenant_id=tenant_id)
                except (ValueError, Subscription.DoesNotExist):

                    return Response({
                        "error": "Invalid tenant or no subscription found."
                    }, status=status.HTTP_400_BAD_REQUEST)

                if subscription.status not in ['active', 'trial']:

                    return Response({
                        "allowed": False,
                        "reason": f"Subscription is {subscription.status}"
                    }, status=status.HTTP_403_FORBIDDEN)

                usage_validator = UsageValidator(request)
                usage_data = usage_validator.validate_usage_limits(str(tenant_id), subscription.plan)

                if not usage_data['valid']:

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

                        return Response({
                            "allowed": False,
                            "reason": f"User limit ({subscription.plan.max_users}) exceeded",
                            "usage": usage_data['usage']
                        }, status=status.HTTP_403_FORBIDDEN)
                

                return Response({
                    "allowed": True,
                    "usage": usage_data['usage'],
                    "plan_limits": {
                        "max_users": subscription.plan.max_users,
                        "max_branches": subscription.plan.max_branches
                    }
                })

            except Exception as e:

                return Response({
                    "error": "Usage validation failed",
                    "message": str(e),
                    "timestamp": timezone.now().isoformat()
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)