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

from .models import Plan, Subscription, TenantBillingPreferences
from apps.payment.models import Payment
from .serializers import (
    SubscriptionSerializer, PaymentSerializer,
    PlanChangeSerializer, AdvanceRenewalSerializer, AutoRenewToggleSerializer
)
from .permissions import CanViewEditSubscription
from .utils import swagger_helper
from .services import SubscriptionService


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


    @action(detail=False, methods=['post'], url_path='toggle-auto-renew')
    @swagger_helper("Customer Portal", "toggle_auto_renew")
    def toggle_auto_renew(self, request):
        """
        NEW SIMPLIFIED VERSION: Toggle auto-renewal using TenantBillingPreferences
        """
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            serializer = AutoRenewToggleSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            auto_renew = serializer.validated_data['auto_renew']

            # Get or create tenant billing preferences (one record per tenant)
            preferences, created = TenantBillingPreferences.objects.get_or_create(
                tenant_id=tenant_id,
                defaults={'user_id': str(request.user.id)}
            )

            # Simply update the auto-renewal setting
            preferences.auto_renew_enabled = auto_renew
            preferences.save()

            message = 'Auto-renew enabled successfully.' if auto_renew else 'Auto-renew disabled successfully.'

            # Get current subscription for response
            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            subscription_data = SubscriptionSerializer(subscription).data if subscription else None

            return Response({
                'data': message,
                'subscription': subscription_data,
                'auto_renew': auto_renew,
                'preferences_updated': True
            })

        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Auto-renew toggle failed: {str(e)}")
            return Response({'error': 'Auto-renew toggle failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='extend')
    @swagger_helper("Customer Portal", "extend")
    def extend(self, request):
        """
        Smart extension endpoint that handles:
        - Emergency extension when < 30 days remaining
        - Advance renewal (pay ahead for next period)
        - Optional plan change effective at end of current period
        - FULLY SUPPORTS CARD UPDATES FOR BOTH PAYSTACK & FLUTTERWAVE
        """
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            if not subscription:
                return Response({'error': 'No subscription found'}, status=status.HTTP_404_NOT_FOUND)

            # Parse request data
            periods = request.data.get('periods', 1)
            new_plan_id = request.data.get('new_plan_id')
            provider = request.data.get('provider', 'paystack').lower()

            # Extract Flutterwave token if provided (for card updates)
            flutterwave_token = request.data.get("flutterwave_token")  # ← FROM FRONTEND

            # Validate periods
            if periods < 1:
                return Response({'error': 'Periods must be at least 1'}, status=status.HTTP_400_BAD_REQUEST)

            # Check business rules
            remaining_days = subscription.get_remaining_days()
            is_advance_renewal = remaining_days >= 30

            # Rule 1: Check for existing advance renewal
            billing_prefs = subscription.tenant_billing_preferences
            if is_advance_renewal and billing_prefs and billing_prefs.next_renewal_date:
                if billing_prefs.next_renewal_date > subscription.end_date:
                    return Response({
                        'error': 'You already have an advance renewal pending. Wait until your current period ends.'
                    }, status=status.HTTP_400_BAD_REQUEST)

            # Rule 2: Emergency extension requires < 30 days
            if not is_advance_renewal and remaining_days >= 30:
                return Response({
                    'error': 'Emergency extension only available when less than 30 days remaining'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Validate subscription status
            if subscription.status not in ['active', 'expired']:
                return Response({
                    'error': f'Subscription cannot be extended (status: {subscription.status})'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Validate new plan if provided
            new_plan = None
            if new_plan_id:
                try:
                    new_plan = Plan.objects.get(id=new_plan_id)
                    if not new_plan.is_active or new_plan.discontinued:
                        return Response({'error': 'New plan is not available'}, status=status.HTTP_400_BAD_REQUEST)
                except Plan.DoesNotExist:
                    return Response({'error': 'New plan does not exist'}, status=status.HTTP_400_BAD_REQUEST)

            # Calculate amount
            plan_for_calculation = new_plan if new_plan else subscription.plan
            from decimal import Decimal
            amount = Decimal(str(plan_for_calculation.price)) * periods

            # Create payment record
            payment = Payment.objects.create(
                plan=plan_for_calculation,
                subscription=subscription,
                amount=amount,
                transaction_id=str(uuid.uuid4()),
                status='pending',
                provider=provider,
                payment_type='extension' if not is_advance_renewal else 'advance_renewal'
            )

            # Handle scheduled plan change
            if new_plan:
                subscription.scheduled_plan = new_plan
                subscription.save()

            # Update next renewal date for advance renewal
            if is_advance_renewal:
                if not billing_prefs:
                    billing_prefs = TenantBillingPreferences.objects.create(
                        tenant_id=tenant_id,
                        user_id=str(request.user.id)
                    )
                from dateutil.relativedelta import relativedelta
                period_mapping = {
                    'monthly': relativedelta(months=periods),
                    'quarterly': relativedelta(months=3*periods),
                    'biannual': relativedelta(months=6*periods),
                    'annual': relativedelta(years=periods),
                }
                delta = period_mapping.get(subscription.plan.billing_period, relativedelta(months=periods))
                billing_prefs.next_renewal_date = subscription.end_date + delta
                billing_prefs.save()

            # Generate metadata (CRITICAL: includes flutterwave_token for card updates)
            metadata = {
                'action': 'extend_subscription' if not is_advance_renewal else 'advance_renewal',
                'subscription_id': str(subscription.id),
                'periods': periods,
                'new_plan_id': str(new_plan.id) if new_plan else None,
                'tenant_id': str(tenant_id),
            }

            # ADD FLUTTERWAVE TOKEN IF PRESENT — THIS MAKES CARD CHANGE WORK
            if flutterwave_token:
                metadata['flutterwave_token'] = flutterwave_token

            # Initialize payment
            if provider == 'paystack':
                from .payments import initiate_paystack_payment
                from .utils import generate_confirm_token
                confirm_token = generate_confirm_token(request.user, str(plan_for_calculation.id))

                response = initiate_paystack_payment(
                    confirm_token=confirm_token,
                    amount=float(amount),
                    user=request.user,
                    plan_id=str(plan_for_calculation.id),
                    tenant_id=str(tenant_id),
                    tenant_name=getattr(request.user, 'tenant_name', None),
                    metadata=metadata
                )

            elif provider == 'flutterwave':
                from .payments import initiate_flutterwave_payment
                from .utils import generate_confirm_token
                confirm_token = generate_confirm_token(request.user, str(plan_for_calculation.id))

                response = initiate_flutterwave_payment(
                    confirm_token=confirm_token,
                    amount=float(amount),
                    user=request.user,
                    plan_id=str(plan_for_calculation.id),
                    tenant_id=str(tenant_id),
                    tenant_name=getattr(request.user, 'tenant_name', None),
                    metadata=metadata  # ← Now includes flutterwave_token
                )
            else:
                payment.delete()
                return Response({'error': f'Unsupported payment provider: {provider}'},
                              status=status.HTTP_400_BAD_REQUEST)

            if response.status_code == 200:
                response_data = response.data
                extension_type = "advance renewal" if is_advance_renewal else "emergency extension"

                return Response({
                    'data': f'Payment initiated for subscription {extension_type}.',
                    'payment_id': str(payment.id),
                    'subscription_id': str(subscription.id),
                    'payment_url': response_data.get('payment_link') or response_data.get('link'),
                    'authorization_url': response_data.get('authorization_url'),
                    'reference': response_data.get('tx_ref'),
                    'amount': str(amount),
                    'provider': provider,
                    'periods': periods,
                    'remaining_days_before': remaining_days,
                    'extension_type': extension_type,
                    'plan_change_scheduled': new_plan.name if new_plan else None,
                    'message': f'Please complete payment to {"renew in advance" if is_advance_renewal else "extend"} your subscription'
                })
            else:
                payment.delete()
                error_data = response.data if hasattr(response, 'data') else {'error': 'Payment initialization failed'}
                return Response(error_data, status=response.status_code)

        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Smart extension failed: {str(e)}", exc_info=True)
            return Response({'error': 'Extension failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['get'], url_path='manage-payment-method')
    @swagger_helper("Customer Portal", "get_manage_payment_method_link")
    def manage_payment_method(self, request):
        """
        Returns the correct URL for user to update their card.
        Paystack: Hosted dashboard (best UX)
        Flutterwave: Force new payment (only way)

        Production-Ready Flow:
        1. User calls this endpoint
        2. Gets Paystack dashboard URL or Flutterwave re-payment instruction
        3. User updates card on provider's platform
        4. Provider sends webhook with new card details
        5. Webhook automatically updates TenantBillingPreferences
        """
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            prefs = TenantBillingPreferences.objects.filter(tenant_id=tenant_id).first()
            if not prefs or not prefs.payment_provider:
                return Response({
                    "error": "No payment method found. Please complete your first payment."
                }, status=status.HTTP_400_BAD_REQUEST)

            if prefs.payment_provider == "paystack":
                if prefs.paystack_subscription_code:
                    manage_url = f"https://paystack.com/my-subscriptions/{prefs.paystack_subscription_code}"
                    return Response({
                        "provider": "paystack",
                        "action": "redirect_to_dashboard",
                        "manage_url": manage_url,
                        "message": "Update your card on Paystack's secure dashboard"
                    })
                else:
                    # Fallback: force new payment
                    return Response({
                        "provider": "paystack",
                        "action": "new_payment_required",
                        "message": "Please make a new payment to update your desired card"
                    })

            elif prefs.payment_provider == "flutterwave":
                return Response({
                    "provider": "flutterwave",
                    "action": "new_payment_required",
                    "message": "Please make any payment (even ₦100) with your new card to update it"
                })

        except Exception as e:
            logger.error(f"manage_payment_method error: {e}")
            return Response({"error": "Failed to generate payment update link"}, status=500)

    @action(detail=False, methods=['get'], url_path='payment-info')
    @swagger_helper("Customer Portal", "get_payment_provider_info")
    def get_payment_provider_info(self, request):
        """
        Get comprehensive payment provider information for the tenant.
        This endpoint provides details about current payment methods and billing setup.

        Data Flow:
        1. Retrieves TenantBillingPreferences for the tenant
        2. Gets last payment information from Payment model
        3. Returns payment method details, card info, and renewal status

        Outcomes:
        - Returns current payment provider (Paystack/Flutterwave)
        - Shows card details (last4, brand) if available
        - Displays auto-renewal status
        - Shows last payment information
        - Provides next renewal date if scheduled

        Use Case:
        - Users checking their current payment setup
        - Frontend displaying payment method in UI
        - Troubleshooting payment issues
        """
        try:
            tenant_id = getattr(request.user, 'tenant', None)
            if not tenant_id:
                return Response({'error': 'No tenant associated with user'}, status=status.HTTP_403_FORBIDDEN)

            # Get tenant billing preferences
            preferences = TenantBillingPreferences.objects.filter(tenant_id=tenant_id).first()

            if not preferences:
                return Response({
                    'auto_renewal': False,
                    'payment_provider': None,
                    'message': 'No billing preferences configured'
                })

            # Get subscription for last payment info
            subscription = Subscription.objects.filter(tenant_id=tenant_id).first()
            last_payment = None
            if subscription:
                last_payment = subscription.payments.filter(status='completed').order_by('-payment_date').first()
                if last_payment:
                    last_payment = {
                        'provider': last_payment.provider,
                        'amount': str(last_payment.amount),
                        'date': last_payment.payment_date.isoformat(),
                        'transaction_id': last_payment.transaction_id
                    }

            return Response({
                'auto_renewal': preferences.auto_renew_enabled,
                'payment_provider': preferences.payment_provider,
                'card_info': {
                    'last4': preferences.card_last4,
                    'brand': preferences.card_brand,
                    'email': preferences.payment_email
                } if preferences.card_last4 else None,
                'last_payment': last_payment,
                'next_renewal_date': preferences.next_renewal_date.isoformat() if preferences.next_renewal_date else None,
                'preferred_plan': str(preferences.preferred_plan.id) if preferences.preferred_plan else None
            })

        except Exception as e:
            print(f"CustomerPortalViewSet.get_payment_provider_info: Unexpected error - {str(e)}")
            return Response({'error': 'Failed to retrieve payment info'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)








