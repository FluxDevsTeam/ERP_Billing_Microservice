from rest_framework import serializers
from .models import Payment
from apps.billing.models import Plan
from apps.billing.utils import IdentityServiceClient


class InitiateSerializer(serializers.Serializer):
    plan_id = serializers.PrimaryKeyRelatedField(queryset=Plan.objects.all())
    provider = serializers.ChoiceField(choices=['paystack', 'flutterwave'])
    auto_renew = serializers.BooleanField(default=False, required=False)


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ['id', 'plan', 'amount', 'payment_date', 'transaction_id', 'status', 'provider']
        read_only_fields = ['amount', 'payment_date', 'transaction_id', 'status', 'provider']


class PaymentSummaryInputSerializer(serializers.Serializer):
    plan_id = serializers.PrimaryKeyRelatedField(queryset=Plan.objects.all())

    def validate(self, attrs):
        request = self.context.get('request')
        plan = attrs.get('plan_id')

        user = getattr(request, 'user', None)
        role = getattr(user, 'role', None)
        role_lc = role.lower() if isinstance(role, str) else None
        is_super = getattr(user, 'is_superuser', False) or role_lc == 'superuser'
        if is_super:
            return attrs

        tenant_id = getattr(user, 'tenant', None)
        if isinstance(tenant_id, dict):
            tenant_id = tenant_id.get('id')
        if not tenant_id:
            raise serializers.ValidationError({"tenant": "No tenant associated with user."})

        client = IdentityServiceClient(request=request)
        tenant = client.get_tenant(tenant_id=str(tenant_id))
        tenant_industry = tenant.get('industry') if isinstance(tenant, dict) else None

        if not tenant_industry:
            raise serializers.ValidationError({"industry": "Could not resolve tenant industry."})

        if plan.industry != tenant_industry:
            raise serializers.ValidationError({"plan_id": "Selected plan is not available for tenant industry."})

        return attrs
