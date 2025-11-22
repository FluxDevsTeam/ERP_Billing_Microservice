# apps/billing/views_customer_portal.py
from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.exceptions import ValidationError
from django.utils import timezone
import uuid
import logging

logger = logging.getLogger(__name__)

from .models import Plan, Subscription, AutoRenewal, RecurringToken
from apps.payment.models import Payment
from .serializers import (
    SubscriptionSerializer, PaymentSerializer,
    PlanChangeSerializer, AdvanceRenewalSerializer, AutoRenewToggleSerializer
)
from .permissions import CanViewEditSubscription
from .utils import swagger_helper
from .services import SubscriptionService, AutoRenewalService


class CustomerPortalViewSet(viewsets.ModelViewSet):
    queryset = Subscription.objects.all()
    permission_classes = [IsAuthenticated, CanViewEditSubscription]

    def get_queryset(self):
        user = self.request.user
        role = getattr(user, 'role', None)
        print(f"CustomerPortalViewSet.get_queryset - User: {user}, Role: {role}")
        if user.is_superuser or (role and role.lower() == 'superuser'):
            print("CustomerPortalViewSet: Superuser accessing all subscriptions")
            return Subscription.objects.select_related('plan', 'scheduled_plan').all()
        tenant_id = getattr(user, 'tenant', None)
        print(f"CustomerPortalViewSet: Tenant ID: {tenant_id}")
        if tenant_id and role and role.lower() == 'ceo':
            try:
                tenant_id = uuid.UUID(str(tenant_id))
                print(f"CustomerPortalViewSet: Filtering subscriptions for tenant_id={tenant_id}")
                return Subscription.objects.select_related('plan', 'scheduled_plan').filter(tenant_id=tenant_id)
            except ValueError:
                print("CustomerPortalViewSet: Invalid tenant ID format")
                return Subscription.objects.none()
        print("CustomerPortalViewSet: No relevant role or tenant, returning empty queryset")
        return Subscription.objects.none()

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

    @action(detail=True, methods=['post'], url_path='extend')
    @swagger_helper("Customer Portal", "extend_subscription")
    def extend_subscription(self, request, pk=None):
        """
        Extend subscription when remaining days is below 30.
        This triggers a payment flow (redirects to payment page) - user must pay to extend.
        After successful payment, subscription will be extended by one billing period.
        """
        try:
            subscription = self.get_object()

            # Validate subscription can be extended
            if subscription.status not in ['active', 'expired']:
                return Response({'error': 'Subscription must be active or expired to extend'},
                              status=status.HTTP_400_BAD_REQUEST)

            remaining_days = subscription.get_remaining_days()
            if remaining_days >= 30:
                return Response({'error': 'Subscription can only be extended when remaining days is less than 30'},
                              status=status.HTTP_400_BAD_REQUEST)

            # Calculate amount (one billing period)
            from decimal import Decimal
            amount = Decimal(str(subscription.plan.price))
            provider = request.data.get('provider', 'paystack')

            # Get tenant information
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                return Response({'error': 'No tenant associated with user'},
                              status=status.HTTP_400_BAD_REQUEST)

            # Create payment record for tracking
            payment = Payment.objects.create(
                plan=subscription.plan,
                subscription=subscription,
                amount=amount,
                transaction_id=str(uuid.uuid4()),
                status='pending',
                provider=provider,
                payment_type='extension'  # Mark as extension payment (will use 'advance' if 'extension' not available)
            )

            # Initialize payment based on provider (same as manual-payment-new-card)
            if provider == 'paystack':
                from .payments import initiate_paystack_payment
                from .utils import generate_confirm_token
                confirm_token = generate_confirm_token(request.user, str(subscription.plan.id))

                response = initiate_paystack_payment(
                    confirm_token=confirm_token,
                    amount=float(amount),
                    user=request.user,
                    plan_id=str(subscription.plan.id),
                    tenant_id=str(tenant_id),
                    tenant_name=getattr(request.user, 'tenant_name', None),
                    metadata={'action': 'extend_subscription', 'subscription_id': str(subscription.id)}
                )

            elif provider == 'flutterwave':
                from .payments import initiate_flutterwave_payment
                from .utils import generate_confirm_token
                confirm_token = generate_confirm_token(request.user, str(subscription.plan.id))

                response = initiate_flutterwave_payment(
                    confirm_token=confirm_token,
                    amount=float(amount),
                    user=request.user,
                    plan_id=str(subscription.plan.id),
                    tenant_id=str(tenant_id),
                    tenant_name=getattr(request.user, 'tenant_name', None),
                    metadata={'action': 'extend_subscription', 'subscription_id': str(subscription.id)}
                )
            else:
                payment.delete()
                return Response({'error': f'Unsupported payment provider: {provider}'},
                              status=status.HTTP_400_BAD_REQUEST)

            if response.status_code == 200:
                response_data = response.data
                return Response({
                    'data': 'Payment initiated for subscription extension. Complete payment to extend your subscription.',
                    'payment_id': str(payment.id),
                    'subscription_id': str(subscription.id),
                    'payment_url': response_data.get('payment_link'),
                    'authorization_url': response_data.get('authorization_url'),
                    'reference': response_data.get('tx_ref'),
                    'amount': str(amount),
                    'provider': provider,
                    'periods': 1,
                    'remaining_days_before': remaining_days,
                    'message': 'Please complete payment to extend your subscription by one billing period'
                })
            else:
                payment.delete()
                error_data = response.data if hasattr(response, 'data') else {'error': 'Payment initialization failed'}
                return Response(error_data, status=response.status_code)

        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Subscription extension failed: {str(e)}")
            return Response({'error': 'Subscription extension failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='renew')
    @swagger_helper("Customer Portal", "renew_subscription")
    def renew_subscription(self, request, pk=None):
        try:
            subscription = self.get_object()
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
            print(f"CustomerPortalViewSet.renew_subscription: Subscription renewed for id={pk}")
            return Response({
                'data': 'Subscription renewed successfully.',
                'subscription': serializer.data
            })

        except ValidationError as e:
            print(f"CustomerPortalViewSet.renew_subscription: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"CustomerPortalViewSet.renew_subscription: Unexpected error - {str(e)}")
            return Response({'error': 'Subscription renewal failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='change-plan')
    @swagger_helper("Customer Portal", "change_plan")
    def change_plan_detail(self, request, pk=None):
        try:
            subscription = self.get_object()
            serializer = PlanChangeSerializer(data=request.data, context={'subscription': subscription})
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
                f"CustomerPortalViewSet.change_plan_detail: Plan changed for subscription_id={pk}, new_plan_id={serializer.validated_data['new_plan_id']}")
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
            print(f"CustomerPortalViewSet.change_plan_detail: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"CustomerPortalViewSet.change_plan_detail: Unexpected error - {str(e)}")
            return Response({'error': 'Plan change failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='advance-renewal')
    @swagger_helper("Customer Portal", "advance_renewal")
    def advance_renewal_detail(self, request, pk=None):
        """
        DEPRECATED: Use /extend/ endpoint instead (when < 30 days remaining) or manual-payment for multiple periods.
        This endpoint triggers payment flow for advance renewal (multiple periods).
        """
        try:
            serializer = AdvanceRenewalSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            subscription = self.get_object()

            periods = serializer.validated_data['periods']
            plan_id = serializer.validated_data.get('plan_id')
            plan = subscription.plan
            if plan_id:
                plan = Plan.objects.get(id=plan_id)

            from decimal import Decimal
            amount = Decimal(str(plan.price)) * periods
            provider = request.data.get('provider', 'paystack')

            # Get tenant information
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                return Response({'error': 'No tenant associated with user'},
                              status=status.HTTP_400_BAD_REQUEST)

            # Create payment record for tracking
            payment = Payment.objects.create(
                plan=plan,
                subscription=subscription,
                amount=amount,
                transaction_id=str(uuid.uuid4()),
                status='pending',
                provider=provider,
                payment_type='advance_renewal',
                metadata={'periods': periods}
            )

            # Initialize payment flow (same as extend/manual-payment-new-card)
            if provider == 'paystack':
                from .payments import initiate_paystack_payment
                from .utils import generate_confirm_token
                confirm_token = generate_confirm_token(request.user, str(plan.id))

                response = initiate_paystack_payment(
                    confirm_token=confirm_token,
                    amount=float(amount),
                    user=request.user,
                    plan_id=str(plan.id),
                    tenant_id=str(tenant_id),
                    tenant_name=getattr(request.user, 'tenant_name', None),
                    metadata={'action': 'advance_renewal', 'periods': periods, 'subscription_id': str(subscription.id)}
                )

            elif provider == 'flutterwave':
                from .payments import initiate_flutterwave_payment
                from .utils import generate_confirm_token
                confirm_token = generate_confirm_token(request.user, str(plan.id))

                response = initiate_flutterwave_payment(
                    confirm_token=confirm_token,
                    amount=float(amount),
                    user=request.user,
                    plan_id=str(plan.id),
                    tenant_id=str(tenant_id),
                    tenant_name=getattr(request.user, 'tenant_name', None),
                    metadata={'action': 'advance_renewal', 'periods': periods, 'subscription_id': str(subscription.id)}
                )
            else:
                payment.delete()
                return Response({'error': f'Unsupported payment provider: {provider}'},
                              status=status.HTTP_400_BAD_REQUEST)

            if response.status_code == 200:
                response_data = response.data
                return Response({
                    'data': 'Advance renewal payment initiated. Complete payment to renew subscription.',
                    'payment_id': str(payment.id),
                    'subscription_id': str(subscription.id),
                    'payment_url': response_data.get('payment_link'),
                    'authorization_url': response_data.get('authorization_url'),
                    'reference': response_data.get('tx_ref'),
                    'amount': str(amount),
                    'periods': periods,
                    'provider': provider,
                    'message': 'Please complete payment to renew subscription in advance'
                })
            else:
                payment.delete()
                error_data = response.data if hasattr(response, 'data') else {'error': 'Payment initialization failed'}
                return Response(error_data, status=response.status_code)

        except ValidationError as e:
            print(f"CustomerPortalViewSet.advance_renewal_detail: Validation error - {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(f"CustomerPortalViewSet.advance_renewal_detail: Unexpected error - {str(e)}")
            return Response({'error': 'Advance renewal failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='toggle-auto-renew')
    @swagger_helper("Customer Portal", "toggle_auto_renew")
    def toggle_auto_renew_detail(self, request, pk=None):
        try:
            serializer = AutoRenewToggleSerializer(data=request.data)
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

    @action(detail=True, methods=['post'], url_path='change-card')
    @swagger_helper("Customer Portal", "change_subscription_card")
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
            print(f"CustomerPortalViewSet.change_subscription_card: Unexpected error - {str(e)}")
            return Response({'error': 'Card change failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'], url_path='payment-info')
    @swagger_helper("Customer Portal", "get_payment_provider_info")
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
            print(f"CustomerPortalViewSet.get_payment_provider_info: Unexpected error - {str(e)}")
            return Response({'error': 'Failed to retrieve payment info'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='update-payment-method')
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
