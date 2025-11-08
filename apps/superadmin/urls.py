# billing_microservice/urls.py
from django.urls import path
from .views import SuperadminPortalViewSet

urlpatterns = [
    path('superadmin/analytics/', SuperadminPortalViewSet.as_view({'get': 'get_analytics'}), name='superadmin_analytics'),
    path('superadmin/subscriptions/', SuperadminPortalViewSet.as_view({'get': 'list_subscriptions'}), name='superadmin_subscription_list'),
    path('superadmin/subscriptions/<uuid:pk>/audit-logs/', SuperadminPortalViewSet.as_view({'get': 'get_subscription_audit_logs'}), name='superadmin_subscription_audit_logs'),
    path('superadmin/audit-logs/', SuperadminPortalViewSet.as_view({'get': 'list_audit_logs'}), name='superadmin_audit_logs'),
    path('superadmin/webhook-events/', SuperadminPortalViewSet.as_view({'get': 'list_webhook_events'}), name='superadmin_webhook_events'),
    path('superadmin/webhook-events/<uuid:pk>/retry/', SuperadminPortalViewSet.as_view({'post': 'retry_webhook'}), name='superadmin_retry_webhook'),
]