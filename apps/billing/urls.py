# billing_microservice/urls.py
from django.urls import path
from .views import (
    PlanView,
    SubscriptionView,
    AccessCheckView,
    CustomerPortalViewSet,
    AutoRenewalViewSet,
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
    path('subscriptions/<uuid:pk>/extend/', SubscriptionView.as_view({'post': 'extend_subscription'}), name='subscription_extend'),
    path('subscriptions/<uuid:pk>/toggle-auto-renew/', SubscriptionView.as_view({'post': 'toggle_auto_renew'}), name='subscription_toggle_auto_renew'),
    path('subscriptions/<uuid:pk>/audit-logs/', SubscriptionView.as_view({'get': 'get_audit_logs'}), name='subscription_audit_logs'),
    # path('subscriptions/check-expired/', SubscriptionView.as_view({'post': 'check_expired_subscriptions'}), name='subscription_check_expired'),
    path('subscriptions/activate-trial/', SubscriptionView.as_view({'post': 'activate_trial'}), name='subscription_activate_trial'),

    # CustomerPortalViewSet
    path('customer-portal/details/', CustomerPortalViewSet.as_view({'get': 'get_subscription_details'}), name='customer_portal_details'),
    path('customer-portal/change-plan/', CustomerPortalViewSet.as_view({'post': 'change_plan'}), name='customer_portal_change_plan'),
    path('customer-portal/advance-renewal/', CustomerPortalViewSet.as_view({'post': 'advance_renewal'}), name='customer_portal_advance_renewal'),
    path('customer-portal/extend/', CustomerPortalViewSet.as_view({'post': 'extend_subscription'}), name='customer_portal_extend'),
    path('customer-portal/toggle-auto-renew/', CustomerPortalViewSet.as_view({'post': 'toggle_auto_renew'}), name='customer_portal_toggle_auto_renew'),

    # AutoRenewalViewSet
    path('auto-renewals/', AutoRenewalViewSet.as_view({'get': 'list', 'post': 'create'}), name='auto_renewal_list_create'),
    path('auto-renewals/<uuid:pk>/', AutoRenewalViewSet.as_view({'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy'}), name='auto_renewal_detail'),
    path('auto-renewals/<uuid:pk>/process/', AutoRenewalViewSet.as_view({'post': 'process_renewal'}), name='auto_renewal_process'),
    path('auto-renewals/<uuid:pk>/cancel/', AutoRenewalViewSet.as_view({'post': 'cancel_renewal'}), name='auto_renewal_cancel'),
    path('auto-renewals/process-due/', AutoRenewalViewSet.as_view({'post': 'process_due_renewals'}), name='auto_renewal_process_due'),

    # AccessCheckView
    path('access-check/', AccessCheckView.as_view({'get': 'list'}), name='access_check'),

    # System Health
    path('health/', SystemHealthView.as_view({'get': 'list'}), name='system_health'),
    path('detailed-health/', SystemHealthView.as_view({'get': 'detailed_health'}), name='detailed_health'),
]