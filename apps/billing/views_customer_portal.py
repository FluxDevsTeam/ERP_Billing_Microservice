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

from .models import Subscription, AutoRenewal
from apps.payment.models import Payment
from .serializers import (
    SubscriptionSerializer, PaymentSerializer,
    PlanChangeSerializer, AdvanceRenewalSerializer, AutoRenewToggleSerializer
)
from .permissions import CanViewEditSubscription
from .utils import swagger_helper
from .services import SubscriptionService, AutoRenewalService


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
