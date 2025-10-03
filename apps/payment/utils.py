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