# billing_microservice/urls.py
from django.urls import path
from .views_plan import PlanView
from .views_subscription import SubscriptionView
from .views_access import AccessCheckView
from .views_customer_portal import CustomerPortalViewSet
from .views_webhook import payment_webhook
from .health_views import SystemHealthView

urlpatterns = [
    # PlanView
    path('plans/', PlanView.as_view({'get': 'list', 'post': 'create'}), name='plan_list_create'),
    path('plans/<uuid:pk>/', PlanView.as_view({'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy'}), name='plan_detail'),
    path('plans/health/', PlanView.as_view({'get': 'health_check'}), name='plan_health_check'),

    # SubscriptionView
    path('subscriptions/', SubscriptionView.as_view({'get': 'list', 'post': 'create'}), name='subscription_list_create'),
    path('subscriptions/<uuid:pk>/', SubscriptionView.as_view({'get': 'retrieve', 'patch': 'partial_update', 'delete': 'destroy'}), name='subscription_detail'),
    path('subscriptions/<uuid:pk>/suspend/', SubscriptionView.as_view({'post': 'suspend_subscription'}), name='subscription_suspend'),
    path('subscriptions/<uuid:pk>/audit-logs/', SubscriptionView.as_view({'get': 'get_audit_logs'}), name='subscription_audit_logs'),
    path('subscriptions/activate-trial/', SubscriptionView.as_view({'post': 'activate_trial'}), name='subscription_activate_trial'),

    # CustomerPortalViewSet
    path('customer-portal/details/', CustomerPortalViewSet.as_view({'get': 'get_subscription_details'}), name='customer_portal_details'),
    path('customer-portal/change-plan/', CustomerPortalViewSet.as_view({'post': 'change_plan'}), name='customer_portal_change_plan'),
    path('customer-portal/toggle-auto-renew/', CustomerPortalViewSet.as_view({'post': 'toggle_auto_renew'}), name='customer_portal_toggle_auto_renew'),
    path('customer-portal/extend/', CustomerPortalViewSet.as_view({'post': 'extend'}), name='customer_portal_extend'),
    path('customer-portal/manage-payment-method/', CustomerPortalViewSet.as_view({'get': 'manage_payment_method'}), name='customer_portal_manage_payment_method'),
    path('customer-portal/payment-info/', CustomerPortalViewSet.as_view({'get': 'get_payment_provider_info'}), name='customer_portal_payment_info'),

    # Webhooks
    path('webhooks/payment/', payment_webhook, name='payment_webhook'),

    # AccessCheckView
    path('access-check/', AccessCheckView.as_view({'get': 'list'}), name='access_check'),

    # System Health
    path('health/', SystemHealthView.as_view({'get': 'list'}), name='system_health'),
    path('detailed-health/', SystemHealthView.as_view({'get': 'detailed_health'}), name='detailed_health'),
]