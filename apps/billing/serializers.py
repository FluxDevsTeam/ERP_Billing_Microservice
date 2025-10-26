# apps/billing/serializers.py
from rest_framework import serializers
from django.utils import timezone
from .models import Plan, Subscription, AuditLog, TrialUsage
from apps.payment.models import Payment
import logging
from .period_calculator import PeriodCalculator

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
    scheduled_plan = PlanSerializer(read_only=True)
    remaining_days = serializers.SerializerMethodField()
    in_grace_period = serializers.SerializerMethodField()
    billing_period_display = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = [
            'id', 'tenant_id', 'plan', 'scheduled_plan', 'status', 'start_date', 'end_date',
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

    def get_billing_period_display(self, obj):
        return PeriodCalculator.get_period_display(obj.plan.billing_period, obj.start_date)

    def get_remaining_days(self, obj):
        return obj.get_remaining_days()

    def get_in_grace_period(self, obj):
        return obj.is_in_grace_period()


class SubscriptionCreateSerializer(serializers.Serializer):
    plan_id = serializers.UUIDField()
    start_date = serializers.DateTimeField(required=False)
    end_date = serializers.DateTimeField(required=False)
    auto_renew = serializers.BooleanField(default=True)

    def validate(self, data):
        user = self.context['request'].user
        tenant_id = getattr(user, 'tenant', None)
        if not tenant_id:
            logger.warning(f"Subscription creation failed: No tenant_id associated with user {user.email}")
            raise serializers.ValidationError("No tenant associated with user")

        # Check existing subscription
        existing_sub = Subscription.objects.filter(
            tenant_id=tenant_id,
            status__in=['active', 'trial', 'pending']
        ).first()
        if existing_sub:
            logger.warning(
                f"Subscription creation blocked: Tenant {tenant_id} already has active subscription {existing_sub.id}")
            raise serializers.ValidationError("Tenant already has an active subscription")

        # Validate plan
        plan_id = data['plan_id']
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

        data['tenant_id'] = tenant_id
        return data


class TrialActivationSerializer(serializers.Serializer):
    plan_id = serializers.UUIDField(required=False)

    def validate(self, data):
        user = self.context['request'].user
        tenant_id = getattr(user, 'tenant', None)
        if not tenant_id:
            logger.warning(f"Trial activation failed: No tenant_id associated with user {user.email}")
            raise serializers.ValidationError("No tenant associated with user")

        # Check existing subscription
        existing_sub = Subscription.objects.filter(
            tenant_id=tenant_id,
            status__in=['active', 'trial', 'pending']
        ).first()
        if existing_sub:
            logger.warning(
                f"Trial activation blocked: Tenant {tenant_id} already has active subscription {existing_sub.id}")
            raise serializers.ValidationError("Tenant already has an active subscription")

        # Validate plan if provided
        plan_id = data.get('plan_id')
        if plan_id:
            try:
                plan = Plan.objects.get(id=plan_id)
                if not plan.is_active or plan.discontinued:
                    logger.warning(f"Trial activation failed: Plan {plan_id} is not available")
                    raise serializers.ValidationError("Plan is not available")
            except Plan.DoesNotExist:
                logger.error(f"Trial activation failed: Plan {plan_id} does not exist")
                raise serializers.ValidationError("Plan does not exist")

        # Trial abuse prevention
        has_previous_trial = TrialUsage.objects.filter(tenant_id=tenant_id, user_email=user.email).exists()
        if has_previous_trial:
            logger.warning(f"Trial abuse detected: Tenant {tenant_id} or user {user.email} already used trial")
            raise serializers.ValidationError("Trial already used for this tenant or user")

        data['tenant_id'] = tenant_id
        return data


class SubscriptionRenewSerializer(serializers.Serializer):
    subscription_id = serializers.UUIDField()

    def validate_subscription_id(self, value):
        try:
            subscription = Subscription.objects.get(id=value)
            if subscription.status not in ['active', 'expired']:
                logger.warning(f"Renewal failed: Subscription {value} is {subscription.status}")
                raise serializers.ValidationError(f"Subscription cannot be renewed (status: {subscription.status})")
            return value
        except Subscription.DoesNotExist:
            logger.error(f"Renewal failed: Subscription {value} does not exist")
            raise serializers.ValidationError("Subscription does not exist")


class SubscriptionSuspendSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, default="Administrative suspension")
    subscription_id = serializers.UUIDField()

    def validate_subscription_id(self, value):
        try:
            subscription = Subscription.objects.get(id=value)
            if subscription.status != 'active':
                logger.warning(f"Suspension failed: Subscription {value} is {subscription.status}")
                raise serializers.ValidationError(f"Subscription cannot be suspended (status: {subscription.status})")
            return value
        except Subscription.DoesNotExist:
            logger.error(f"Suspension failed: Subscription {value} does not exist")
            raise serializers.ValidationError("Subscription does not exist")


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
        fields = ['id', 'plan', 'subscription', 'amount', 'payment_date', 'transaction_id', 'status', 'provider',
                  'payment_type']
        read_only_fields = ['amount', 'payment_date', 'transaction_id', 'status', 'provider', 'payment_type']
