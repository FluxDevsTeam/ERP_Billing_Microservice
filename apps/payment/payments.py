import uuid
import requests
from rest_framework import status
from rest_framework.response import Response
from django.conf import settings


def initiate_flutterwave_payment(confirm_token, amount, user, plan_id, tenant_id, tenant_name=None):
    try:
        flutterwave_key = settings.PAYMENT_PROVIDERS["flutterwave"]["secret_key"]
        url = "https://api.flutterwave.com/v3/payments"
        headers = {"Authorization": f"Bearer {flutterwave_key}"}
        first_name = tenant_name or user.first_name or ""
        last_name = user.last_name or ""
        phone_no = user.phone_number or ""
        reference = str(uuid.uuid4())
        base_url = settings.BILLING_MICROSERVICE_URL
        redirect_url = f"{base_url}/api/v1/payment/payment-verify/confirm/?tx_ref={reference}&confirm_token={confirm_token}&provider=flutterwave&amount={amount}&plan_id={plan_id}&tenant_id={tenant_id}"
        data = {
            "tx_ref": reference,
            "amount": str(amount),
            "currency": settings.PAYMENT_CURRENCY,
            "redirect_url": redirect_url,
            "meta": {"consumer_id": user.id, "plan_id": plan_id, "tenant_id": tenant_id},
            "customer": {
                "email": user.email,
                "phonenumber": phone_no,
                "name": f"{last_name} {first_name}"
            },
            "customizations": {
                "title": "ERP Subscription Payment",
                "logo": "https://tse3.mm.bing.net/th/id/OIP._08Ei4c5bwrBSNNLsoWMhgHaHa?cb=12&rs=1&pid=ImgDetMain&o=7&rm=3"
            },
            "configurations": {
                "session_duration": 10,
                "max_retry_attempt": 5
            },
        }

        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        response_data = response.json()

        payment_link = response_data.get("data", {}).get("link")
        if not payment_link:
            return Response({"error": "Payment processing error. Please try again."}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({
            "message": "Flutterwave payment initiated successfully.",
            "payment_link": payment_link,
            "tx_ref": reference,
            "authorization_url": payment_link
        }, status=status.HTTP_200_OK)

    except requests.exceptions.RequestException:
        return Response({"error": "Payment service unavailable. Please try again later."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception:
        return Response({"error": "Payment processing failed. Please try again."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def initiate_paystack_payment(confirm_token, amount, user, plan_id, tenant_id, tenant_name=None):
    try:
        paystack_key = settings.PAYMENT_PROVIDERS['paystack']['secret_key']
        headers = {"Authorization": f"Bearer {paystack_key}", "Content-Type": "application/json"}
        url = "https://api.paystack.co/transaction/initialize"
        first_name = tenant_name or user.first_name or ""
        last_name = user.last_name or ""
        phone_no = user.phone_number or ""
        reference = str(uuid.uuid4())
        base_url = settings.BILLING_MICROSERVICE_URL
        callback_url = f"{base_url}/api/v1/payment/payment-verify/confirm/?tx_ref={reference}&confirm_token={confirm_token}&provider=paystack&amount={amount}&plan_id={plan_id}&tenant_id={tenant_id}"
        data = {
            "amount": int(amount * 100),
            "email": user.email,
            "currency": settings.PAYMENT_CURRENCY,
            "reference": reference,
            "callback_url": callback_url,
            "metadata": {
                "consumer_id": user.id,
                "plan_id": plan_id,
                "tenant_id": tenant_id
            }
        }
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        response_data = response.json()

        if not response_data.get("status"):
            error_msg = response_data.get("message", "Payment initiation failed")
            return Response({"error": error_msg}, status=response.status_code)

        payment_link = response_data.get("data", {}).get("authorization_url")
        if not payment_link:
            return Response({"error": "Payment processing error. Please try again."}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({
            "message": "Paystack payment initiated successfully.",
            "payment_link": payment_link,
            "tx_ref": reference,
            "authorization_url": payment_link
        }, status=status.HTTP_200_OK)

    except requests.exceptions.RequestException:
        return Response({"error": "Payment service unavailable. Please try again later."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception:
        return Response({"error": "Payment processing failed. Please try again."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
