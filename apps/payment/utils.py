from rest_framework_simplejwt.tokens import RefreshToken
from django.utils.timezone import now
from datetime import timedelta
import requests
from django.conf import settings

def generate_confirm_token(user, subscription_id):
    try:
        refresh = RefreshToken.for_user(user)
        refresh['subscription_id'] = subscription_id
        refresh['exp'] = int((now() + timedelta(hours=1)).timestamp())
        return str(refresh.access_token)
    except Exception as e:
        raise

def initiate_refund(provider, amount, user, transaction_id):
    try:
        if provider == "paystack":
            payload = {"transaction": transaction_id}
            headers = {
                "Authorization": f"Bearer {settings.PAYMENT_PROVIDERS['paystack']['secret_key']}",
                "Content-Type": "application/json"
            }
            response = requests.post(
                "https://api.paystack.co/refund",
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            return True
        elif provider == "flutterwave":
            if not transaction_id:
                return False

            url = f"https://api.flutterwave.com/v3/transactions/{transaction_id}/refund"
            headers = {
                "Authorization": f"Bearer {settings.PAYMENT_PROVIDERS['flutterwave']['secret_key']}",
                "Content-Type": "application/json"
            }
            payload = {}
            response = requests.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return True
    except:
        try:
            return "admin"
        except:
            return False
from drf_yasg.utils import swagger_auto_schema
from .pagination import PAGINATION_PARAMS


def swagger_helper(tags, model):
    def decorators(func):
        descriptions = {
            "list": f"Retrieve a list of {model}",
            "retrieve": f"Retrieve details of a specific {model}",
            "create": f"Create a new {model}",
            "partial_update": f"Update a {model}",
            "destroy": f"Delete a {model}",
            "renew_subscription": f"Renew a {model}",
            "suspend_subscription": f"Suspend a {model}",
            "change_plan": f"Change plan for a {model}",
            "advance_renewal": f"Advance renewal for a {model}",
            "toggle_auto_renew": f"Toggle auto-renew for a {model}",
            "check_expired_subscriptions": f"Check expired {model}",
            "get_audit_logs": f"Get audit logs for a {model}",
            "get_subscription_details": f"Get {model} details",
            "get_analytics": f"Get analytics data",
            "list_subscriptions": f"List all {model}",
            "get_subscription_audit_logs": f"Get {model} audit logs",
            "retry_webhook": f"Retry webhook event",
            "list_webhook_events": f"List webhook events",
            "create_payment_summary": f"Create payment summary",
            "initiate_payment": f"Initiate payment",
            "confirm_payment": f"Confirm payment",
            "handle_webhook": f"Handle payment webhook",
            "refund_payment": f"Refund payment",
        }

        action_type = func.__name__
        get_description = descriptions.get(action_type, f"{action_type} {model}")
        return swagger_auto_schema(manual_parameters=PAGINATION_PARAMS, operation_id=f"{action_type} {model}", operation_description=get_description, tags=[tags])(func)

    return decorators
