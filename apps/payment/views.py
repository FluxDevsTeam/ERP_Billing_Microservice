from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.decorators import action
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.csrf import csrf_exempt
from django.core.exceptions import ValidationError
import requests
import hmac
import hashlib
from django.conf import settings
from django.db import transaction
from rest_framework_simplejwt.tokens import AccessToken

from apps.billing.serializers import SubscriptionSerializer
from .models import Payment
from apps.billing.models import Subscription, Plan
from .permissions import CanInitiatePayment
from .serializers import PaymentSerializer, InitiateSerializer, PaymentSummaryInputSerializer
from .payments import initiate_flutterwave_payment, initiate_paystack_payment
from .utils import initiate_refund, swagger_helper, generate_confirm_token
from apps.billing.utils import IdentityServiceClient
import uuid
from django.utils import timezone
from .services import PaymentService
from api.email_service import send_email_via_service
from ..billing.period_calculator import PeriodCalculator


class PaymentRefundViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    @swagger_helper(tags=['Payment'], model='Payment Refund')
    def create(self, request, pk=None):
        try:
            payment_service = PaymentService(request)
            reason = request.data.get('reason', 'Refund requested')
            result = payment_service.refund_payment(payment_id=pk, reason=reason)

            if result['status'] == 'success':
                # Send refund confirmation email
                payment = Payment.objects.get(id=pk)
                email_data = {
                    'user_email': request.user.email,
                    'email_type': 'confirmation',
                    'subject': 'Refund Processed',
                    'message': f'Your refund request for payment {pk} has been processed. Reason: {reason}',
                    'action': 'Refund Processed'
                }
                send_email_via_service(email_data)

            return Response(result,
                            status=status.HTTP_200_OK if result['status'] == 'success' else status.HTTP_400_BAD_REQUEST)

        except ValidationError as e:

            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:

            return Response({'error': 'Refund processing failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PaymentSummaryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = PaymentSummaryInputSerializer

    @swagger_helper(tags=['Payment'], model='Payment Summary')
    def create(self, request):
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            plan = serializer.validated_data['plan_id']
            if plan.discontinued:
                return Response({"error", "plan is discontinued"}, status=status.HTTP_423_LOCKED)
            # Tenant context
            tenant_id_str = getattr(request.user, 'tenant', None)
            tenant_name = getattr(request.user, 'tenant_name', None)
            active_subscription_info = None
            plan_switch_info = None
            restriction_info = None

            now = timezone.now()
            dummy_subscription = Subscription(plan=plan, start_date=now)
            renewal_end_date = dummy_subscription.calculate_end_date(now)

            data = {
                "plan": {
                    "id": str(plan.id),
                    "name": plan.name,
                    "price": str(plan.price),
                    "billing_period": plan.billing_period,
                }
            }

            # If we have tenant context, enrich summary
            if tenant_id_str:
                try:
                    tenant_uuid = uuid.UUID(str(tenant_id_str))
                    subscription = Subscription.objects.filter(tenant_id=tenant_uuid).first()
                except Exception:
                    subscription = None

                client = IdentityServiceClient(request=request)
                users = client.get_users(tenant_id=str(tenant_id_str))
                branches = client.get_branches(tenant_id=str(tenant_id_str))
                current_users_count = len(users) if isinstance(users, list) else 0
                current_branches_count = len(branches) if isinstance(branches, list) else 0

                # Check user and branch limits against desired plan
                restrictions = []
                if current_users_count > plan.max_users:
                    restrictions.append(
                        f"Current number of users ({current_users_count}) exceeds plan limit ({plan.max_users}). "
                        "Please upgrade to a plan with a higher user limit or delete some users to proceed."
                    )
                if current_branches_count > plan.max_branches:
                    restrictions.append(
                        f"Current number of branches ({current_branches_count}) exceeds plan limit ({plan.max_branches}). "
                        "Please upgrade to a plan with a higher branch limit or delete some branches to proceed."
                    )

                if restrictions:
                    restriction_info = {
                        "restricted": True,
                        "reasons": restrictions,
                        "current_users": current_users_count,
                        "allowed_users": plan.max_users,
                        "current_branches": current_branches_count,
                        "allowed_branches": plan.max_branches,
                    }

                if subscription:
                    is_active = subscription.status == 'active' and subscription.end_date and subscription.end_date > now
                    effective_base = subscription.end_date if is_active else now
                    active_subscription_info = {
                        "has_active_subscription": is_active,
                        "current_plan_id": str(subscription.plan.id),
                        "current_plan_name": subscription.plan.name,
                        "expires_on": subscription.end_date,
                        "days_remaining": max((subscription.end_date - now).days, 0) if subscription.end_date else 0,
                        "renewal_effective_end_date": renewal_end_date,
                    }

                    if str(subscription.plan.id) != str(plan.id):
                        plan_switch_info = {
                            "is_switch": True,
                            "from_plan_id": str(subscription.plan.id),
                            "from_plan_name": subscription.plan.name,
                            "to_plan_id": str(plan.id),
                            "to_plan_name": plan.name,
                        }

            if active_subscription_info is not None:
                data["active_subscription"] = active_subscription_info
            if plan_switch_info is not None:
                data["plan_switch"] = plan_switch_info
            if restriction_info is not None:
                data["restriction"] = restriction_info
            return Response(data)
        except Exception as e:
            return Response({"error": f"Could not generate payment summary: {str(e)}"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PaymentInitiateViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, CanInitiatePayment]
    serializer_class = InitiateSerializer

    @swagger_helper(tags=['Payment'], model='Payment Initiate')
    def create(self, request):
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            plan = serializer.validated_data['plan_id']

            if plan.discontinued:
                return Response({"error", "plan is discontinued"}, status=status.HTTP_423_LOCKED)

            provider = serializer.validated_data['provider']
            amount = plan.price
            tenant_id = getattr(request.user, 'tenant', None)
            tenant_name = getattr(request.user, 'tenant_name', None)
            token = generate_confirm_token(request.user, str(plan.id))
            # Restrict switching if user or branch count exceeds new plan limit
            if tenant_id:
                try:
                    tenant_uuid = uuid.UUID(str(tenant_id))
                except Exception:
                    tenant_uuid = None
                if tenant_uuid is not None:
                    existing_sub = Subscription.objects.filter(tenant_id=tenant_uuid).first()
                    # Only restrict when switching to a different plan
                    if existing_sub and str(existing_sub.plan.id) != str(plan.id):
                        client = IdentityServiceClient(request=request)
                        users = client.get_users(tenant_id=str(tenant_id))
                        branches = client.get_branches(tenant_id=str(tenant_id))
                        current_users_count = len(users) if isinstance(users, list) else 0
                        current_branches_count = len(branches) if isinstance(branches, list) else 0

                        restrictions = []
                        if current_users_count > plan.max_users:
                            restrictions.append(
                                f"Cannot switch plan: current number of users ({current_users_count}) exceeds plan limit ({plan.max_users}). "
                                "Please upgrade to a plan with a higher user limit or delete some users to proceed."
                            )
                        if current_branches_count > plan.max_branches:
                            restrictions.append(
                                f"Cannot switch plan: current number of branches ({current_branches_count}) exceeds plan limit ({plan.max_branches}). "
                                "Please upgrade to a plan with a higher branch limit or delete some branches to proceed."
                            )

                        if restrictions:
                            return Response({
                                "error": " ".join(restrictions),
                                "current_users": current_users_count,
                                "allowed_users": plan.max_users,
                                "current_branches": current_branches_count,
                                "allowed_branches": plan.max_branches
                            }, status=status.HTTP_400_BAD_REQUEST)
            payment = Payment.objects.create(
                plan=plan,
                amount=amount,
                transaction_id=str(uuid.uuid4()),
                status='pending',
                provider=provider
            )
            if provider == "flutterwave":
                response = initiate_flutterwave_payment(token, amount, request.user, str(plan.id), str(tenant_id),
                                                        tenant_name)
            elif provider == "paystack":
                response = initiate_paystack_payment(token, amount, request.user, str(plan.id), str(tenant_id),
                                                     tenant_name)
            else:
                return Response({"error": "Invalid payment provider"}, status=status.HTTP_400_BAD_REQUEST)

            if response.status_code == 200:
                payment.transaction_id = response.data.get('tx_ref')
                payment.save()

                # Send payment initiation email
                email_data = {
                    'user_email': request.user.email,
                    'email_type': 'confirmation',
                    'subject': 'Payment Initiated',
                    'message': f'Your payment of {amount} for {plan.name} plan has been initiated. Please complete the payment process.',
                    'action': 'Payment Initiated',
                    'link': response.data.get('authorization_url', ''),
                    'link_text': 'Complete Payment'
                }
                send_email_via_service(email_data)
            else:
                payment.status = 'failed'
                payment.save()
                return Response(response.data, status=response.status_code)

            return Response({"data": response.data}, status=response.status_code)

        except Exception as e:
            return Response({"error": f"Payment initiation failed: {str(e)}"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PaymentVerifyViewSet(viewsets.ViewSet):
    permission_classes = [AllowAny]

    @swagger_helper(tags=['Payment'], model='Payment Verify')
    @transaction.atomic
    @action(detail=False, methods=['get'])
    def confirm(self, request):
        try:
            tx_ref = request.query_params.get("tx_ref")
            amount = float(request.query_params.get("amount"))
            provider = request.query_params.get("provider")
            token = request.query_params.get("confirm_token")
            plan_id_param = request.query_params.get("plan_id")
            tenant_id_param = request.query_params.get("tenant_id")
            transaction_id = request.query_params.get("transaction_id") or tx_ref

            if not all([tx_ref, amount, provider, token, plan_id_param, tenant_id_param]):
                return redirect("https://example.com/payment-failed/?data=Invalid-request-parameters")

            try:
                decoded = AccessToken(token)
                plan_id_from_token = decoded.get("subscription_id")
                if str(plan_id_from_token) != plan_id_param:
                    raise ValueError("Plan ID mismatch")

                plan = get_object_or_404(Plan, id=plan_id_from_token)
                user = request.user if request.user.is_authenticated else None
            except Exception:
                return redirect(f"{settings.FRONTEND_PATH}/payment-failed/?data=Invalid-token-or-subscription")

            existing_payment = Payment.objects.filter(transaction_id=tx_ref).first()
            if existing_payment and existing_payment.status == 'completed':
                return redirect(f"https://example.com/plan/{plan.id}")

            provider_config = settings.PAYMENT_PROVIDERS[provider]
            url = provider_config["verify_url"].format(transaction_id)
            headers = {
                "Authorization": f"Bearer {provider_config['secret_key']}",
                "Content-Type": "application/json"
            }

            try:
                verification_response = requests.get(url, headers=headers, timeout=10)
                verification_response.raise_for_status()
            except requests.exceptions.RequestException:
                return redirect(f"{settings.FRONTEND_PATH}/payment-failed/?data=Payment-verification-failed")

            response_data = verification_response.json()
            expected_currency = settings.PAYMENT_CURRENCY
            flutterwave_transaction_id = None

            if provider == "flutterwave":
                verification_success = (
                        response_data.get("status") == "success" and
                        response_data["data"]["status"] == "successful" and
                        float(response_data["data"]["amount"]) >= float(amount) and
                        response_data["data"]["currency"] == expected_currency and
                        response_data["data"]["tx_ref"] == tx_ref
                )
                flutterwave_transaction_id = str(response_data["data"]["id"]) if verification_success else None
            else:
                verification_success = (
                        response_data.get("status") and
                        response_data["data"]["status"] == "success" and
                        (response_data["data"]["amount"] / 100) >= float(amount) and
                        response_data["data"]["currency"] == expected_currency
                )

            if not verification_success:
                payment = Payment.objects.filter(transaction_id=tx_ref).first()
                if payment:
                    payment.status = 'failed'
                    payment.save()
                    # Send payment failure email
                    if user and user.email:
                        email_data = {
                            'user_email': user.email,
                            'email_type': 'general',
                            'subject': 'Payment Failed',
                            'message': 'Your payment could not be verified. Please try again or contact support.',
                            'action': 'Payment Failed'
                        }
                        send_email_via_service(email_data)
                return redirect(f"{settings.FRONTEND_PATH}/payment-failed/?data=Payment-verification-failed")

            payment = Payment.objects.get(transaction_id=tx_ref, plan=plan)
            payment.status = 'completed'
            payment.transaction_id = flutterwave_transaction_id if provider == "flutterwave" else transaction_id
            payment.save()

            # Create or update subscription for tenant
            try:
                tenant_uuid = uuid.UUID(tenant_id_param)
            except Exception:
                return redirect(f"{settings.FRONTEND_PATH}/payment-failed/?data=Invalid-tenant")

            current_time = timezone.now()
            subscription, created = Subscription.objects.get_or_create(
                tenant_id=tenant_uuid,
                defaults={
                    'plan': plan,
                    'status': 'active',
                    'start_date': current_time,
                }
            )

            if not created:
                # For existing subscriptions
                if subscription.plan != plan:
                    subscription.plan = plan
                subscription.status = 'active'
                # If current subscription is still active, start from its end date
                if subscription.end_date and subscription.end_date > current_time:
                    subscription.start_date = subscription.end_date + timezone.timedelta(days=1)
                else:
                    # If expired, start fresh from current time
                    subscription.start_date = current_time

            # Let the model calculate the end_date based on the billing period
            subscription.end_date = None
            subscription.save()

            # Send payment success email
            if user and user.email:
                email_data = {
                    'user_email': user.email,
                    'email_type': 'confirmation',
                    'subject': 'Payment Successful',
                    'message': f'Your payment of {amount} has been processed successfully. Your subscription to {plan.name} plan has been activated/renewed.',
                    'action': 'Payment Successful'
                }
                send_email_via_service(email_data)

            return redirect(f"{settings.FRONTEND_PATH}/subscription/{subscription.id}")

        except Exception as e:
            return redirect(f"{settings.FRONTEND_PATH}/payment-failed/?data=Payment-processing-failed")


class PaymentWebhookViewSet(viewsets.ViewSet):
    permission_classes = [AllowAny]

    @swagger_helper(tags=['Payment'], model='Payment Webhook')
    @transaction.atomic
    @action(detail=False, methods=['post'])
    @csrf_exempt
    def create(self, request):
        try:
            if "HTTP_VERIF_HASH" in request.META:
                provider = "flutterwave"
                signature = request.META["HTTP_VERIF_HASH"]
                secret_hash = settings.PAYMENT_PROVIDERS["flutterwave"]["secret_hash"]
                if signature != secret_hash:
                    return Response({"error": "Invalid signature"}, status=401)
            elif "HTTP_X_PAYSTACK_SIGNATURE" in request.META:
                provider = "paystack"
                signature = request.META["HTTP_X_PAYSTACK_SIGNATURE"]
                secret_key = settings.PAYMENT_PROVIDERS["paystack"]["secret_key"]
                expected_signature = hmac.new(secret_key.encode(), request.body, hashlib.sha512).hexdigest()
                if not hmac.compare_digest(signature, expected_signature):
                    return Response({"error": "Invalid signature"}, status=403)
            else:
                return Response({"error": "Unknown provider"}, status=400)

            payload = request.data

            if provider == "flutterwave":
                if payload.get("event") != "charge.completed":
                    return Response({"message": "Event ignored"}, status=200)
                data = payload.get("data", {})
                tx_ref = data.get("tx_ref")
                transaction_id = str(data.get("id")) if data.get("id") is not None else None
                status = data.get("status") == "successful"
                amount = float(data.get("amount", 0))
                email = data.get("customer", {}).get("email")
                currency = data.get("currency")
                plan_id = data.get("meta", {}).get("plan_id")
                tenant_id_param = data.get("meta", {}).get("tenant_id")
            else:
                if payload.get("event") != "charge.success":
                    return Response({"message": "Event ignored"}, status=200)
                data = payload.get("data", {})
                tx_ref = data.get("reference")
                transaction_id = tx_ref
                status = data.get("status") == "success"
                amount = float(data.get("amount", 0)) / 100
                email = data.get("customer", {}).get("email")
                currency = data.get("currency")
                plan_id = data.get("metadata", {}).get("plan_id")
                tenant_id_param = data.get("metadata", {}).get("tenant_id")

            if not all([tx_ref, amount, email, plan_id, tenant_id_param]):
                return Response({"error": "Missing transaction reference, amount, email, plan_id, or tenant_id"},
                                status=400)
            plan = Plan.objects.filter(id=plan_id).first()
            if not plan:
                return Response({"error": "Plan not found"}, status=400)

            payment = Payment.objects.filter(transaction_id=tx_ref, plan=plan).first()
            if payment and payment.status == 'completed':
                return Response({"message": "Transaction already processed"}, status=200)

            if not status:
                if payment:
                    payment.status = 'failed'
                    payment.save()
                    # Send payment failure email
                    email_data = {
                        'user_email': email,
                        'email_type': 'general',
                        'subject': 'Payment Failed',
                        'message': f'Your payment of {amount} {currency} could not be processed. Please try again or contact support.',
                        'action': 'Payment Failed'
                    }
                    send_email_via_service(email_data)
                return Response({"message": "Payment not successful"}, status=200)

            if currency != settings.PAYMENT_CURRENCY:
                return Response({"error": "Currency not supported"}, status=400)

            provider_config = settings.PAYMENT_PROVIDERS[provider]
            url = provider_config["verify_url"].format(transaction_id)
            headers = {
                "Authorization": f"Bearer {provider_config['secret_key']}",
                "Content-Type": "application/json"
            }

            try:
                verification_response = requests.get(url, headers=headers, timeout=10)
                verification_response.raise_for_status()
            except requests.exceptions.RequestException:
                return Response({"error": "Payment verification failed"}, status=503)

            response_data = verification_response.json()
            expected_currency = settings.PAYMENT_CURRENCY
            flutterwave_transaction_id = None

            if provider == "flutterwave":
                verification_success = (
                        response_data.get("status") == "success" and
                        response_data["data"]["status"] == "successful" and
                        float(response_data["data"]["amount"]) >= float(amount) and
                        response_data["data"]["currency"] == expected_currency and
                        str(response_data["data"]["tx_ref"]) == str(tx_ref)
                )
                flutterwave_transaction_id = str(response_data["data"]["id"]) if verification_success else None
            else:
                verification_success = (
                        response_data.get("status") and
                        response_data["data"]["status"] == "success" and
                        (float(response_data["data"]["amount"]) / 100) >= float(amount) and
                        response_data["data"]["currency"] == expected_currency and
                        str(response_data["data"]["reference"]) == str(tx_ref)
                )

            if not verification_success:
                if payment:
                    payment.status = 'failed'
                    payment.save()
                return Response({"error": "Payment verification failed"}, status=400)

            if not payment:
                payment = Payment.objects.create(
                    plan=plan,
                    amount=amount,
                    transaction_id=flutterwave_transaction_id if provider == "flutterwave" else transaction_id,
                    status='completed',
                    provider=provider
                )
            else:
                payment.status = 'completed'
                payment.transaction_id = flutterwave_transaction_id if provider == "flutterwave" else transaction_id
                payment.save()

            try:
                tenant_uuid = uuid.UUID(tenant_id_param)
            except Exception:
                return Response({"error": "Invalid tenant id"}, status=400)

            current_time = timezone.now()
            subscription, created = Subscription.objects.get_or_create(
                tenant_id=tenant_uuid,
                defaults={
                    'plan': plan,
                    'status': 'active',
                    'start_date': current_time,
                }
            )

            if not created:
                # For existing subscriptions
                if subscription.plan != plan:
                    subscription.plan = plan
                subscription.status = 'active'
                # If current subscription is still active, start from its end date
                if subscription.end_date and subscription.end_date > current_time:
                    subscription.start_date = subscription.end_date + timezone.timedelta(days=1)
                else:
                    # If expired, start fresh from current time
                    subscription.start_date = current_time

            # Let the model calculate the end_date based on the billing period
            subscription.end_date = None  # Trigger recalculation
            subscription.save()

            # Send payment success email
            email_data = {
                'user_email': email,
                'email_type': 'confirmation',
                'subject': 'Payment Successful',
                'message': f'Your payment of {amount} has been processed successfully. Your subscription has been activated/renewed.',
                'action': 'Payment Successful'
            }
            send_email_via_service(email_data)

            return Response({"message": "Webhook processed"}, status=200)

        except Exception as e:
            return Response({"error": f"Webhook processing failed: {str(e)}"}, status=500)
