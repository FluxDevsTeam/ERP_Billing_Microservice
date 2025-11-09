from rest_framework import serializers
from apps.payment.models import WebhookEvent

class AnalyticsSerializer(serializers.Serializer):
    mrr = serializers.FloatField()
    churn_rate = serializers.FloatField()
    trial_conversion_rate = serializers.FloatField()
    failed_payments_rate = serializers.FloatField()
    arpu = serializers.FloatField()
    customers_by_industry = serializers.ListField(child=serializers.DictField())
    renewal_success_rate = serializers.FloatField()
    revenue_by_plan = serializers.ListField(child=serializers.DictField())
    active_plans = serializers.ListField(child=serializers.DictField())
    total_subscriptions = serializers.IntegerField()
    active_subscriptions = serializers.IntegerField()
    expired_subscriptions = serializers.IntegerField()
    trial_subscriptions = serializers.IntegerField()
    total_payments = serializers.IntegerField()
    completed_payments = serializers.IntegerField()
    failed_payments = serializers.IntegerField()
    payment_methods = serializers.ListField(child=serializers.DictField())
    audit_log_summary = serializers.ListField(child=serializers.DictField())
    timestamp = serializers.CharField()
    # New metrics requested
    total_users = serializers.IntegerField()
    total_revenue_generated = serializers.FloatField()
    active_users_by_plan = serializers.ListField(child=serializers.DictField())

class WebhookEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookEvent
        fields = ['id', 'provider', 'event_type', 'payload', 'status', 'retry_count', 'max_retries', 'last_retry_at', 'created_at', 'error_message']