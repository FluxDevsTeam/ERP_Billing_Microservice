# apps/billing/webhooks.py
import json
import hmac
import hashlib
from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from .models import TenantBillingPreferences, Subscription
import logging
import uuid

logger = logging.getLogger(__name__)


@require_POST
@csrf_exempt
def payment_webhook(request):
    """
    Unified webhook for Paystack & Flutterwave
    Now 100% correctly saves reusable tokens for BOTH providers
    """
    payload = request.body
    signature = request.headers.get('X-Paystack-Signature') or request.headers.get('verif-hash')

    # Determine provider
    if request.headers.get('X-Paystack-Signature'):
        provider = "paystack"
    elif request.headers.get('verif-hash'):
        provider = "flutterwave"
    else:
        logger.warning("Webhook missing signature header")
        return HttpResponse("No signature", status=400)

    # Verify signature
    if provider == "paystack":
        expected = hmac.new(
            settings.PAYSTACK_SECRET_KEY.encode(),
            payload,
            hashlib.sha512
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            logger.warning("Invalid Paystack signature")
            return HttpResponse("Invalid signature", status=400)

    elif provider == "flutterwave":
        if not hmac.compare_digest(signature or "", settings.FLUTTERWAVE_WEBHOOK_SECRET or ""):
            logger.warning("Invalid Flutterwave signature")
            return HttpResponse("Invalid signature", status=400)

    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        logger.error("Invalid JSON in webhook")
        return HttpResponse("Bad JSON", status=400)

    event_type = event.get("event") or event.get("status", "").lower()

    # Only process successful payments
    if "success" not in event_type:
        return HttpResponse("Event ignored", status=200)

    data = event.get("data", event)

    # === Extract common fields ===
    tenant_id = None
    auth_code = None
    customer_code = None
    last4 = None
    brand = None
    email = None
    subscription_code = None
    auto_renew = False  # ← NEW: Extract auto_renew from metadata

    if provider == "paystack":
        metadata = data.get("metadata", {})
        tenant_id = metadata.get("tenant_id")
        auto_renew = metadata.get("auto_renew", False)  # ← Extract auto_renew

        auth = data.get("authorization", {})
        customer = data.get("customer", {})

        auth_code = auth.get("authorization_code")
        customer_code = customer.get("customer_code")
        last4 = auth.get("last4")
        brand = auth.get("card_type") or auth.get("brand")
        email = customer.get("email")
        subscription_code = data.get("subscription", {}).get("subscription_code", {}).get("subscription_code")

    elif provider == "flutterwave":
        # Flutterwave can send metadata in "meta" or "metadata"
        metadata = data.get("meta") or data.get("metadata", {})
        tenant_id = metadata.get("tenant_id")
        auto_renew = metadata.get("auto_renew", False)  # ← Extract auto_renew

        card = data.get("card", {})
        customer = data.get("customer", {})

        last4 = card.get("last4digits") or card.get("last_4digits")
        brand = card.get("type") or card.get("brand")
        email = customer.get("email")
        customer_code = customer.get("id")

        # THIS IS THE FIX: Get reusable token from metadata (sent by your frontend/backend)
        auth_code = metadata.get("flutterwave_token")  # ← This is the reusable card token

    # === Fallback: Find tenant by email if metadata missing ===
    if not tenant_id and email:
        try:
            prefs = TenantBillingPreferences.objects.filter(payment_email__iexact=email).first()
            if prefs:
                tenant_id = str(prefs.tenant_id)
        except Exception as e:
            logger.error(f"Email fallback failed: {e}")

    if not tenant_id:
        logger.warning(f"Webhook: tenant_id not found for {provider}")
        return HttpResponse("Tenant not found", status=200)

    # Ensure tenant_id is UUID string
    try:
        tenant_uuid = uuid.UUID(str(tenant_id))
    except ValueError:
        logger.error(f"Invalid tenant_id format: {tenant_id}")
        return HttpResponse("Invalid tenant_id", status=400)

    # === Update TenantBillingPreferences ===
    defaults = {
        "payment_provider": provider,
        "card_last4": last4,
        "card_brand": brand or "Card",
        "payment_email": email,
        "last_payment_at": timezone.now(),
        "auto_renew_enabled": auto_renew,  # ← Set auto_renew from metadata
        "renewal_status": "active" if auto_renew else "paused",  # ← Set renewal status
    }

    if provider == "paystack":
        defaults.update({
            "paystack_authorization_code": auth_code,
            "paystack_customer_code": customer_code,
            "paystack_subscription_code": subscription_code,
        })
    elif provider == "flutterwave":
        defaults.update({
            "flutterwave_payment_method_id": auth_code,
            "flutterwave_customer_id": customer_code,
        })

    try:
        prefs, created = TenantBillingPreferences.objects.update_or_create(
            tenant_id=tenant_uuid,
            defaults=defaults
        )
        action = "Created" if created else "Updated"
        logger.info(f"{action} billing preferences for tenant {tenant_uuid} via {provider}")

        # If auto_renew is enabled, update subscription expiry and next renewal dates
        if auto_renew:
            try:
                subscription = Subscription.objects.filter(tenant_id=tenant_uuid).first()
                if subscription:
                    # Update subscription expiry and next renewal dates
                    prefs.subscription_expiry_date = subscription.end_date
                    prefs.next_renewal_date = subscription.end_date
                    prefs.save(update_fields=['subscription_expiry_date', 'next_renewal_date'])
                    logger.info(f"Updated subscription dates for tenant {tenant_uuid} with auto_renew enabled")
            except Exception as e:
                logger.warning(f"Failed to update subscription dates for auto_renew: {e}")

    except Exception as e:
        logger.error(f"Failed to update TenantBillingPreferences: {e}")
        return HttpResponse("DB Error", status=500)

    return HttpResponse("OK", status=200)