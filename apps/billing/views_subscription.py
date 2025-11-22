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

logger = logging.getLogger(__name__)

from .models import Plan, Subscription, AuditLog, AutoRenewal, RecurringToken
from apps.payment.models import Payment
from .serializers import (
    SubscriptionSerializer, PaymentSerializer,
    SubscriptionCreateSerializer, TrialActivationSerializer,
    SubscriptionRenewSerializer, SubscriptionSuspendSerializer,
    PlanChangeSerializer, AdvanceRenewalSerializer, AutoRenewToggleSerializer, AuditLogSerializer
)
from .permissions import IsSuperuser, IsCEOorSuperuser, CanViewEditSubscription
from .utils import IdentityServiceClient, swagger_helper
from .services import SubscriptionService, AutoRenewalService
from api.email_service import send_email_via_service


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

    @action(detail=True, methods=['post'], url_path='update-payment-method', permission_classes=[IsAuthenticated])
    def update_payment_method(self, request, pk=None):
        """
        Endpoint to initiate update of payment card for recurring billing.
        If Paystack: returns a manage link (hosted), if Flutterwave: triggers frontend re-entry of card.
        """
        try:
            subscription = self.get_object()
            token = getattr(subscription, 'recurring_token', None)
            if not token:
                return Response({"error": "No recurring token/payment method on file. Make first payment."}, status=400)

            if token.provider == "paystack":
                if token.paystack_subscription_code:
                    url = f'https://dashboard.paystack.com/#/subscriptions/{token.paystack_subscription_code}'
                    return Response({"provider": "paystack", "update_url": url})
                else:
                    return Response({"error": "No Paystack subscription code available for manage link."}, status=400)
            elif token.provider == "flutterwave":
                return Response({"provider": "flutterwave", "update_card_flow": True})

            return Response({"error": "Unknown payment provider."}, status=400)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

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
