from rest_framework import serializers
from apps.payment.models import WebhookEvent

class AnalyticsSerializer(serializers.Serializer):
    mrr = serializers.FloatField()
    churn_rate = serializers.FloatField()
    trial_conversion_rate = serializers.FloatField()
    revenue_by_plan = serializers.ListField(child=serializers.DictField())
    total_subscriptions = serializers.IntegerField()
    active_subscriptions = serializers.IntegerField()
    timestamp = serializers.CharField()

class WebhookEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookEvent
        fields = ['id', 'provider', 'event_type', 'payload', 'status', 'retry_count', 'max_retries', 'last_retry_at', 'created_at', 'error_message']