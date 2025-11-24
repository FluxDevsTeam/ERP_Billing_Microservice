from rest_framework import serializers
from apps.payment.models import WebhookEvent

class AnalyticsSummarySerializer(serializers.Serializer):
    total_revenue = serializers.FloatField()
    period_revenue = serializers.FloatField()
    monthly_recurring_revenue = serializers.FloatField()
    average_revenue_per_user = serializers.FloatField()
    customer_lifetime_value = serializers.FloatField()
    total_users = serializers.IntegerField()
    active_users = serializers.IntegerField()
    churn_rate = serializers.FloatField()
    payment_success_rate = serializers.FloatField()
    trial_conversion_rate = serializers.FloatField()
    auto_renewal_rate = serializers.FloatField()

class RevenueMetricsSerializer(serializers.Serializer):
    total_all_time = serializers.FloatField()
    in_period = serializers.FloatField()
    by_status = serializers.ListField(child=serializers.DictField())
    average_transaction_value = serializers.FloatField()

class RecurringRevenueSerializer(serializers.Serializer):
    mrr = serializers.FloatField()
    breakdown_by_billing_period = serializers.DictField()
    arpu = serializers.FloatField()
    clv = serializers.FloatField()
    projected_mrr_3months = serializers.FloatField()

class FinancialMetricsSerializer(serializers.Serializer):
    revenue = RevenueMetricsSerializer()
    recurring_revenue = RecurringRevenueSerializer()

class SubscriptionStatusSerializer(serializers.Serializer):
    status = serializers.CharField()
    count = serializers.IntegerField()
    percentage = serializers.FloatField()

class ActiveByPlanSerializer(serializers.Serializer):
    plan__name = serializers.CharField()
    plan__tier_level = serializers.CharField()
    count = serializers.IntegerField()
    total_mrr = serializers.FloatField()

class ChurnAnalysisSerializer(serializers.Serializer):
    rate = serializers.FloatField()
    cancellations_in_period = serializers.IntegerField()
    by_plan = serializers.ListField(child=serializers.DictField())

class PlanChangesSerializer(serializers.Serializer):
    upgrades = serializers.IntegerField()
    downgrades = serializers.IntegerField()
    net_change = serializers.IntegerField()

class SubscriptionMetricsSerializer(serializers.Serializer):
    status_breakdown = serializers.ListField(child=SubscriptionStatusSerializer())
    active_by_plan = serializers.ListField(child=ActiveByPlanSerializer())
    churn_analysis = ChurnAnalysisSerializer()
    plan_changes = PlanChangesSerializer()
    active_subscriptions = serializers.IntegerField()
    trial_subscriptions = serializers.IntegerField()

class PaymentSuccessRatesSerializer(serializers.Serializer):
    overall_success_rate = serializers.FloatField()
    failure_rate = serializers.FloatField()
    pending_rate = serializers.FloatField()

class PaymentMethodsPerformanceSerializer(serializers.Serializer):
    provider = serializers.CharField()
    total_count = serializers.IntegerField()
    success_count = serializers.IntegerField()
    failed_count = serializers.IntegerField()
    total_amount = serializers.FloatField()
    success_rate = serializers.FloatField()

class OutstandingReceivablesSerializer(serializers.Serializer):
    amount = serializers.FloatField()
    count = serializers.IntegerField()

class PaymentMetricsSerializer(serializers.Serializer):
    success_rates = PaymentSuccessRatesSerializer()
    by_provider = serializers.ListField(child=PaymentMethodsPerformanceSerializer())
    outstanding_receivables = OutstandingReceivablesSerializer()
    total_transactions = serializers.IntegerField()
    completed_transactions = serializers.IntegerField()
    failed_transactions = serializers.IntegerField()

class UserGrowthTrendSerializer(serializers.Serializer):
    month = serializers.CharField()
    new_users = serializers.IntegerField()

class TrialMetricsSerializer(serializers.Serializer):
    currently_active = serializers.IntegerField()
    started_in_period = serializers.IntegerField()
    converted_in_period = serializers.IntegerField()
    conversion_rate = serializers.FloatField()
    total_trial_signups = serializers.IntegerField()
    unique_trial_users = serializers.IntegerField()

class AutoRenewalSerializer(serializers.Serializer):
    enabled = serializers.IntegerField()
    disabled = serializers.IntegerField()
    failure_count = serializers.IntegerField()

class UserEngagementSerializer(serializers.Serializer):
    growth_trends = serializers.ListField(child=UserGrowthTrendSerializer())
    trial_metrics = TrialMetricsSerializer()
    auto_renewal = AutoRenewalSerializer()

class PlanPerformanceSerializer(serializers.Serializer):
    name = serializers.CharField()
    price = serializers.FloatField()
    billing_period = serializers.CharField()
    tier_level = serializers.CharField()
    industry = serializers.CharField()
    total_subscriptions = serializers.IntegerField()
    active_subscriptions = serializers.IntegerField()
    trial_subscriptions = serializers.IntegerField()
    canceled_subscriptions = serializers.IntegerField()
    total_revenue = serializers.FloatField()
    churn_rate = serializers.FloatField()
    conversion_rate = serializers.FloatField()

class AuditLogSummarySerializer(serializers.Serializer):
    action = serializers.CharField()
    count = serializers.IntegerField()

class SystemHealthSerializer(serializers.Serializer):
    active_subscriptions_ratio = serializers.FloatField()
    payment_processing_efficiency = serializers.FloatField()
    renewal_failure_rate = serializers.FloatField()

class OperationalEfficiencySerializer(serializers.Serializer):
    audit_log_summary = serializers.ListField(child=AuditLogSummarySerializer())
    system_health = SystemHealthSerializer()

class PeriodSerializer(serializers.Serializer):
    start_date = serializers.CharField()
    end_date = serializers.CharField()
    days = serializers.IntegerField()

class AnalyticsSerializer(serializers.Serializer):
    summary = AnalyticsSummarySerializer()
    financial_metrics = FinancialMetricsSerializer()
    subscription_metrics = SubscriptionMetricsSerializer()
    payment_metrics = PaymentMetricsSerializer()
    user_engagement = UserEngagementSerializer()
    plan_performance = serializers.ListField(child=PlanPerformanceSerializer())
    operational_efficiency = OperationalEfficiencySerializer()
    period = PeriodSerializer()
    generated_at = serializers.CharField()
    data_freshness = serializers.CharField()

class WebhookEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookEvent
        fields = ['id', 'provider', 'event_type', 'payload', 'status', 'retry_count', 'max_retries', 'last_retry_at', 'created_at', 'error_message']