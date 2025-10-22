from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.conf import settings
import redis
import requests

import hashlib
import uuid
from typing import Dict, Any, Tuple
from apps.billing.models import Subscription, AuditLog
from .models import Payment, WebhookEvent



class PaymentService:
    def __init__(self, request=None):
        self.request = request
        self.redis_client = redis.Redis.from_url(settings.REDIS_URL)

    @transaction.atomic
    def create_payment(self, subscription_id: str, amount: float, provider: str, payment_type: str = 'initial') -> Tuple[Payment, Dict[str, Any]]:
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            if subscription.status not in ['active', 'trial', 'pending']:

                raise ValidationError("Cannot create payment for non-active subscription")

            transaction_id = f"TXN-{uuid.uuid4()}"
            payment = Payment.objects.create(
                plan=subscription.plan,
                subscription=subscription,
                amount=amount,
                provider=provider,
                transaction_id=transaction_id,
                payment_type=payment_type
            )

            AuditLog.objects.create(
                subscription=subscription,
                action='created',
                user=self._get_user(),
                details={
                    'payment_id': str(payment.id),
                    'transaction_id': transaction_id,
                    'amount': float(amount),
                    'provider': provider,
                    'payment_type': payment_type
                },
                ip_address=self._get_client_ip()
            )


            return payment, {
                'status': 'success',
                'transaction_id': transaction_id,
                'payment_id': str(payment.id),
                'redirect_url': self._generate_payment_url(payment)
            }

        except Subscription.DoesNotExist:

            raise ValidationError("Subscription not found")
        except Exception as e:

            raise ValidationError(f"Payment creation failed: {str(e)}")

    def verify_payment(self, transaction_id: str, provider: str) -> Dict[str, Any]:
        lock_key = f"payment_verify_{transaction_id}"
        with self.redis_client.lock(lock_key, timeout=30):
            try:
                payment = Payment.objects.get(transaction_id=transaction_id)
                if payment.status == 'completed':

                    return {'status': 'success', 'payment_id': str(payment.id)}

                provider_config = settings.PAYMENT_PROVIDERS.get(provider)
                if not provider_config:

                    raise ValidationError("Invalid payment provider")

                verify_url = provider_config['verify_url'].format(transaction_id)
                headers = {'Authorization': f"Bearer {provider_config['secret_key']}"}
                response = requests.get(verify_url, headers=headers, timeout=10)
                response.raise_for_status()
                data = response.json()

                if provider == 'flutterwave':
                    if data['status'] == 'success' and data['data']['status'] == 'successful':
                        payment.status = 'completed'
                        payment.payment_date = timezone.now()
                        payment.save()
                        self._update_subscription(payment)

                        return {'status': 'success', 'payment_id': str(payment.id)}
                elif provider == 'paystack':
                    if data['status'] and data['data']['status'] == 'success':
                        payment.status = 'completed'
                        payment.payment_date = timezone.now()
                        payment.save()
                        self._update_subscription(payment)

                        return {'status': 'success', 'payment_id': str(payment.id)}

                payment.status = 'failed'
                payment.save()

                return {'status': 'error', 'message': 'Payment verification failed'}

            except Payment.DoesNotExist:

                raise ValidationError("Transaction not found")
            except Exception as e:

                raise ValidationError(f"Payment verification failed: {str(e)}")

    def process_webhook(self, provider: str, payload: Dict[str, Any], signature: str) -> Dict[str, Any]:
        try:
            if not self._verify_webhook_signature(provider, payload, signature):

                return {'status': 'error', 'message': 'Invalid webhook signature'}

            event_id = payload.get('event_id', str(uuid.uuid4()))
            webhook_event = WebhookEvent.objects.create(
                provider=provider,
                event_type=payload.get('event'),
                payload=payload
            )

            transaction_id = payload.get('transaction_id') or payload.get('data', {}).get('tx_ref')
            if not transaction_id:
                webhook_event.status = 'failed'
                webhook_event.error_message = 'Missing transaction ID'
                webhook_event.save()

                return {'status': 'error', 'message': 'Missing transaction ID'}

            result = self.verify_payment(transaction_id, provider)
            webhook_event.status = 'processed' if result['status'] == 'success' else 'failed'
            webhook_event.error_message = result.get('message')
            webhook_event.save()


            return result

        except Exception as e:

            WebhookEvent.objects.create(
                provider=provider,
                event_type=payload.get('event', 'unknown'),
                payload=payload,
                status='failed',
                error_message=str(e)
            )
            return {'status': 'error', 'message': str(e)}

    def retry_webhook(self, webhook_event_id: str) -> Dict[str, Any]:
        try:
            webhook_event = WebhookEvent.objects.get(id=webhook_event_id)
            if webhook_event.retry_count >= webhook_event.max_retries:

                return {'status': 'error', 'message': 'Max retries reached'}

            webhook_event.retry_count += 1
            webhook_event.last_retry_at = timezone.now()
            result = self.process_webhook(
                provider=webhook_event.provider,
                payload=webhook_event.payload,
                signature=webhook_event.payload.get('signature', '')
            )

            webhook_event.status = 'processed' if result['status'] == 'success' else 'failed'
            webhook_event.error_message = result.get('message')
            webhook_event.save()


            return result

        except WebhookEvent.DoesNotExist:

            return {'status': 'error', 'message': 'Webhook event not found'}
        except Exception as e:

            return {'status': 'error', 'message': str(e)}

    def refund_payment(self, payment_id: str, reason: str = None) -> Dict[str, Any]:
        if not settings.ENABLE_REFUNDS:

            return {'status': 'error', 'message': 'Refunds are not allowed'}

        try:
            payment = Payment.objects.get(id=payment_id)
            if payment.status != 'completed':

                return {'status': 'error', 'message': 'Cannot refund non-completed payment'}

            # Placeholder for refund logic (not implemented due to ENABLE_REFUNDS=False)

            return {'status': 'success', 'message': 'Refund processed (simulated)'}

        except Payment.DoesNotExist:

            return {'status': 'error', 'message': 'Payment not found'}
        except Exception as e:

            return {'status': 'error', 'message': str(e)}

    def _verify_webhook_signature(self, provider: str, payload: Dict[str, Any], signature: str) -> bool:
        try:
            provider_config = settings.PAYMENT_PROVIDERS.get(provider)
            if not provider_config:
                return False

            if provider == 'flutterwave':
                expected_signature = hashlib.sha256(
                    f"{provider_config['secret_hash']}{str(payload)}".encode()
                ).hexdigest()
                return signature == expected_signature
            elif provider == 'paystack':
                # Paystack uses HMAC-SHA512
                expected_signature = hashlib.sha512(
                    f"{provider_config['secret_key']}{str(payload)}".encode()
                ).hexdigest()
                return signature == expected_signature
            return False
        except Exception as e:

            return False

    def _update_subscription(self, payment: Payment):
        subscription = payment.subscription
        if not subscription:
            return

        subscription.status = 'active'
        subscription.last_payment_date = payment.payment_date
        subscription.next_payment_date = subscription.end_date
        subscription.payment_retry_count = 0
        subscription.save()

        AuditLog.objects.create(
            subscription=subscription,
            action='updated',
            user=self._get_user(),
            details={
                'payment_id': str(payment.id),
                'transaction_id': payment.transaction_id,
                'status': 'Payment verified'
            },
            ip_address=self._get_client_ip()
        )

    def _generate_payment_url(self, payment: Payment) -> str:
        provider_config = settings.PAYMENT_PROVIDERS.get(payment.provider)
        base_url = provider_config.get('payment_url', 'https://api.provider.com/pay')
        return f"{base_url}?transaction_id={payment.transaction_id}&amount={payment.amount}"

    def _get_user(self) -> str:
        return str(self.request.user.id) if self.request and self.request.user else 'system'

    def _get_client_ip(self) -> str:
        if self.request:
            x_forwarded_for = self.request.META.get('HTTP_X_FORWARDED_FOR')
            if x_forwarded_for:
                return x_forwarded_for.split(',')[0]
            return self.request.META.get('REMOTE_ADDR')
        return None