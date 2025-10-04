# billing_microservice/urls.py
from django.urls import path
from .views import (
    PlanView,
    SubscriptionView,
    AccessCheckView,
    CustomerPortalViewSet,
)
from .health_views import SystemHealthView

urlpatterns = [
    # PlanView
    path('plans/', PlanView.as_view({'get': 'list', 'post': 'create'}), name='plan_list_create'),
    path('plans/<uuid:pk>/', PlanView.as_view({'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy'}), name='plan_detail'),
    path('plans/health/', PlanView.as_view({'get': 'health_check'}), name='plan_health_check'),

    # SubscriptionView
    path('subscriptions/', SubscriptionView.as_view({'get': 'list', 'post': 'create'}), name='subscription_list_create'),
    path('subscriptions/<uuid:pk>/', SubscriptionView.as_view({'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy'}), name='subscription_detail'),
    path('subscriptions/<uuid:pk>/renew/', SubscriptionView.as_view({'post': 'renew_subscription'}), name='subscription_renew'),
    path('subscriptions/<uuid:pk>/suspend/', SubscriptionView.as_view({'post': 'suspend_subscription'}), name='subscription_suspend'),
    path('subscriptions/<uuid:pk>/change-plan/', SubscriptionView.as_view({'post': 'change_plan'}), name='subscription_change_plan'),
    path('subscriptions/<uuid:pk>/advance-renewal/', SubscriptionView.as_view({'post': 'advance_renewal'}), name='subscription_advance_renewal'),
    path('subscriptions/<uuid:pk>/toggle-auto-renew/', SubscriptionView.as_view({'post': 'toggle_auto_renew'}), name='subscription_toggle_auto_renew'),
    path('subscriptions/<uuid:pk>/audit-logs/', SubscriptionView.as_view({'get': 'get_audit_logs'}), name='subscription_audit_logs'),
    path('subscriptions/check-expired/', SubscriptionView.as_view({'post': 'check_expired_subscriptions'}), name='subscription_check_expired'),

    # CustomerPortalViewSet
    path('customer-portal/details/', CustomerPortalViewSet.as_view({'get': 'get_subscription_details'}), name='customer_portal_details'),
    path('customer-portal/change-plan/', CustomerPortalViewSet.as_view({'post': 'change_plan'}), name='customer_portal_change_plan'),
    path('customer-portal/advance-renewal/', CustomerPortalViewSet.as_view({'post': 'advance_renewal'}), name='customer_portal_advance_renewal'),
    path('customer-portal/toggle-auto-renew/', CustomerPortalViewSet.as_view({'post': 'toggle_auto_renew'}), name='customer_portal_toggle_auto_renew'),

    # AccessCheckView
    path('access-check/', AccessCheckView.as_view({'get': 'list'}), name='access_check'),
    path('access-check/limits/', AccessCheckView.as_view({'get': 'check_limits'}), name='check_limits'),
    path('access-check/health/', AccessCheckView.as_view({'get': 'health_check'}), name='access_health_check'),
    path('access-check/validate-usage/', AccessCheckView.as_view({'post': 'validate_usage'}), name='validate_usage'),

    # System Health
    path('health/', SystemHealthView.as_view({'get': 'list'}), name='system_health'),
    path('detailed-health/', SystemHealthView.as_view({'get': 'detailed_health'}), name='detailed_health'),
]