from rest_framework import serializers
from django.utils import timezone
from .models import Plan, Subscription, AuditLog, TrialUsage
from apps.payment.models import Payment
import logging

from .utils.period_calculator import PeriodCalculator

logger = logging.getLogger('billing')


class PlanSerializer(serializers.ModelSerializer):
    billing_period_display = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = [
            'id', 'name', 'description', 'industry', 'max_users', 'max_branches', 'price',
            'billing_period', 'billing_period_display', 'is_active', 'discontinued', 'tier_level',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def get_billing_period_display(self, obj):
        return PeriodCalculator.get_period_display(obj.billing_period)


class SubscriptionSerializer(serializers.ModelSerializer):
    plan = PlanSerializer(read_only=True)
    plan_id = serializers.PrimaryKeyRelatedField(queryset=Plan.objects.all(), source='plan', write_only=True)
    scheduled_plan = PlanSerializer(read_only=True)
    scheduled_plan_id = serializers.PrimaryKeyRelatedField(queryset=Plan.objects.all(), source='scheduled_plan', write_only=True, allow_null=True)
    remaining_days = serializers.SerializerMethodField()
    in_grace_period = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = [
            'id', 'tenant_id', 'plan', 'plan_id', 'scheduled_plan', 'scheduled_plan_id', 'status', 'start_date', 'end_date',
            'created_at', 'updated_at', 'suspended_at', 'canceled_at', 'auto_renew',
            'trial_end_date', 'last_payment_date', 'next_payment_date',
            'payment_retry_count', 'max_payment_retries', 'remaining_days', 'in_grace_period',
            'is_first_time_subscription', 'trial_used', 'billing_period_display'
        ]
        read_only_fields = [
            'status', 'start_date', 'end_date', 'created_at', 'updated_at',
            'suspended_at', 'canceled_at', 'last_payment_date', 'next_payment_date',
            'payment_retry_count', 'remaining_days', 'in_grace_period'
        ]

    billing_period_display = serializers.SerializerMethodField()

    def get_billing_period_display(self, obj):
        from apps.billing.utils.period_calculator import PeriodCalculator
        return PeriodCalculator.get_period_display(obj.plan.billing_period, obj.start_date)

    def get_remaining_days(self, obj):
        return obj.get_remaining_days()

    def get_in_grace_period(self, obj):
        return obj.is_in_grace_period()


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = [
            'id', 'action', 'user', 'details', 'timestamp', 'ip_address'
        ]
        read_only_fields = ['id', 'timestamp']


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ['id', 'plan', 'subscription', 'amount', 'payment_date', 'transaction_id', 'status', 'provider', 'payment_type']
        read_only_fields = ['amount', 'payment_date', 'transaction_id', 'status', 'provider', 'payment_type']


class SubscriptionCreateSerializer(serializers.Serializer):
    tenant_id = serializers.UUIDField()
    plan_id = serializers.UUIDField()
    start_date = serializers.DateTimeField(required=False)
    end_date = serializers.DateTimeField(required=False)
    auto_renew = serializers.BooleanField(default=True)

    def validate(self, data):
        tenant_id = data['tenant_id']
        plan_id = data['plan_id']
        user = self.context['request'].user

        # Check existing subscription
        existing_sub = Subscription.objects.filter(
            tenant_id=tenant_id,
            status__in=['active', 'trial', 'pending']
        ).first()
        if existing_sub:
            logger.warning(f"Subscription creation blocked: Tenant {tenant_id} already has active subscription {existing_sub.id}")
            raise serializers.ValidationError("Tenant already has an active subscription")

        # Validate plan
        try:
            plan = Plan.objects.get(id=plan_id)
            if not plan.is_active or plan.discontinued:
                logger.warning(f"Subscription creation failed: Plan {plan_id} is not available")
                raise serializers.ValidationError("Plan is not available")
        except Plan.DoesNotExist:
            logger.error(f"Subscription creation failed: Plan {plan_id} does not exist")
            raise serializers.ValidationError("Plan does not exist")

        # Trial abuse prevention
        has_previous_trial = TrialUsage.objects.filter(tenant_id=tenant_id, user_email=user.email).exists()
        if has_previous_trial:
            logger.warning(f"Trial abuse detected: Tenant {tenant_id} or user {user.email} already used trial")
            raise serializers.ValidationError("Trial already used for this tenant or user")

        return data


class PlanChangeSerializer(serializers.Serializer):
    new_plan_id = serializers.UUIDField()
    immediate = serializers.BooleanField(default=False)
    reason = serializers.CharField(max_length=500, required=False)

    def validate(self, data):
        new_plan_id = data['new_plan_id']
        subscription = self.context['subscription']
        current_plan = subscription.plan

        # Validate new plan
        try:
            new_plan = Plan.objects.get(id=new_plan_id)
            if not new_plan.is_active or new_plan.discontinued:
                logger.warning(f"Plan change failed: New plan {new_plan_id} is not available")
                raise serializers.ValidationError("New plan is not available")
        except Plan.DoesNotExist:
            logger.error(f"Plan change failed: New plan {new_plan_id} does not exist")
            raise serializers.ValidationError("New plan does not exist")

        # No-downgrade policy
        now = timezone.now()
        if subscription.status == 'active' and subscription.end_date > now:
            if new_plan.tier_level < current_plan.tier_level and (now - subscription.start_date).days > 2:
                logger.warning(f"Downgrade blocked: New plan {new_plan.name} has lower tier than {current_plan.name}")
                raise serializers.ValidationError("Downgrades are not allowed during active subscription period")

        return data


class AdvanceRenewalSerializer(serializers.Serializer):
    plan_id = serializers.UUIDField(required=False)
    periods = serializers.IntegerField(min_value=1, default=1)

    def validate_plan_id(self, value):
        if value:
            try:
                plan = Plan.objects.get(id=value)
                if not plan.is_active or plan.discontinued:
                    logger.warning(f"Advance renewal failed: Plan {value} is not available")
                    raise serializers.ValidationError("Plan is not available")
                return value
            except Plan.DoesNotExist:
                logger.error(f"Advance renewal failed: Plan {value} does not exist")
                raise serializers.ValidationError("Plan does not exist")
        return None


class AutoRenewToggleSerializer(serializers.Serializer):
    auto_renew = serializers.BooleanField()