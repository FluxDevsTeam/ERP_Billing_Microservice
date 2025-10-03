# payment/urls.py
from django.urls import path
from .views import (
    PaymentSummaryViewSet,
    PaymentInitiateViewSet,
    PaymentVerifyViewSet,
    PaymentWebhookViewSet,
    PaymentRefundViewSet, 
)

urlpatterns = [
    path('payment-summary/', PaymentSummaryViewSet.as_view({'post': 'create'}), name='payment_summary'),
    path('payment-initiate/', PaymentInitiateViewSet.as_view({'post': 'create'}), name='payment_initiate'),
    path('payment-verify/confirm/', PaymentVerifyViewSet.as_view({'get': 'confirm'}), name='payment_verify_confirm'),
    path('payment-webhook/', PaymentWebhookViewSet.as_view({'post': 'create'}), name='payment_webhook'),
    path('payment-refund/<uuid:pk>/', PaymentRefundViewSet.as_view({'post': 'create'}), name='payment_refund'),
]