from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.conf import settings
import uuid


TIER_CHOICES = (
    ('tier1', 'Tier 1 - Small Business (1-10 users, 1 branch)'),
    ('tier2', 'Tier 2 - Medium Business (11-50 users, 2-5 branches)'),
    ('tier3', 'Tier 3 - Large Enterprise (51-200 users, 6-20 branches)'),
    ('tier4', 'Tier 4 - Global Corporation (201+ users, 21+ branches)'),
)


class AuditLog(models.Model):
    """Audit log for tracking subscription changes"""
    ACTION_CHOICES = (
        ('created', 'Created'),
        ('updated', 'Updated'),
        ('deleted', 'Deleted'),
        ('activated', 'Activated'),
        ('deactivated', 'Deactivated'),
        ('expired', 'Expired'),
        ('renewed', 'Renewed'),
        ('canceled', 'Canceled'),
        ('suspended', 'Suspended'),
        ('plan_changed', 'Plan Changed'),
        ('advance_renewed', 'Advance Renewed'),
        ('auto_renew_toggled', 'Auto-Renew Toggled'),
        ('proration_credited', 'Proration Credited'),
        ('auto_renew_processed', 'Auto-Renew Processed'),
        ('auto_renew_failed', 'Auto-Renew Failed'),
        ('plan_deprecated_handled', 'Plan Deprecated Handled'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey('Subscription', on_delete=models.CASCADE, related_name='audit_logs')
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    user = models.CharField(max_length=255, null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.action} - {self.subscription.tenant_id} at {self.timestamp}"


class Plan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    INDUSTRY_CHOICES = (
        ("Basic", "Basic"),
        ("Finance", "Finance"),
        ("Healthcare", "Healthcare"),
        ("Production", "Production"),
        ("Education", "Education"),
        ("Technology", "Technology"),
        ("Retail", "Retail"),
        ("Agriculture", "Agriculture"),
        ("Real Estate", "Real Estate"),
        ("Supermarket", "Supermarket"),
        ("Warehouse", "Warehouse"),
    )

    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, help_text="Detailed description of the plan and its features")
    industry = models.CharField(max_length=100, choices=INDUSTRY_CHOICES, default="Other")
    max_users = models.IntegerField(default=10)
    max_branches = models.IntegerField(default=2)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    PERIOD_CHOICES = (
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly (3 months)'),
        ('biannual', 'Bi-annual (6 months)'),
        ('annual', 'Annual (12 months)'),
    )
    billing_period = models.CharField(max_length=10, choices=PERIOD_CHOICES, default='monthly')
    is_active = models.BooleanField(default=True)
    discontinued = models.BooleanField(default=False)
    tier_level = models.CharField(max_length=10, choices=TIER_CHOICES, default='tier1')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("name", "industry")
        indexes = [
            models.Index(fields=['industry', 'is_active', 'discontinued']),
        ]

    def clean(self):
        if self.max_users <= 0:
            raise ValidationError("Max users must be greater than 0")
        if self.max_branches <= 0:
            raise ValidationError("Max branches must be greater than 0")
        if self.price < 0:
            raise ValidationError("Price cannot be negative")

    def __str__(self):
        return f"{self.name} ({self.industry})"


class Subscription(models.Model):
    STATUS_CHOICES = (
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('canceled', 'Canceled'),
        ('suspended', 'Suspended'),
        ('pending', 'Pending'),
        ('trial', 'Trial'),
    )
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(unique=True, default=uuid.uuid4, db_index=True)
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE)
    scheduled_plan = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, blank=True, related_name='scheduled_subscriptions')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='trial', db_index=True)
    start_date = models.DateTimeField(default=timezone.now, db_index=True)
    end_date = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    trial_end_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['tenant_id', 'status']),
            models.Index(fields=['status', 'end_date']),
            models.Index(fields=['tenant_id', 'end_date']),
        ]

    def calculate_end_date(self, start_date):
        """Calculate the end date based on the billing period"""
        from dateutil.relativedelta import relativedelta
        
        if self.status == 'trial' and not self.trial_end_date:
            # Trial period remains 7 days
            return start_date + timezone.timedelta(days=7)
            
        period_mapping = {
            'monthly': relativedelta(months=1),
            'quarterly': relativedelta(months=3),
            'biannual': relativedelta(months=6),
            'annual': relativedelta(years=1),
        }
        
        # Get the relative time delta based on billing period
        delta = period_mapping.get(self.plan.billing_period)
        if not delta:
            # Fallback to monthly if period is not recognized
            delta = relativedelta(months=1)
            
        # Calculate end date (one day before the same date next period)
        end_date = start_date + delta - relativedelta(days=1)
        return end_date

    def save(self, *args, **kwargs):
        if not self.end_date:
            if self.status == 'trial' and not self.trial_end_date:
                self.trial_end_date = self.calculate_end_date(self.start_date)
                self.end_date = self.trial_end_date
            else:
                self.end_date = self.calculate_end_date(self.start_date)
        
        if self.end_date < timezone.now() and self.status not in ['canceled', 'suspended']:
            self.status = 'expired'
        
        super().save(*args, **kwargs)

    def is_in_grace_period(self):
        if self.status == 'expired':
            grace_end = self.end_date + timezone.timedelta(days=settings.SUBSCRIPTION_GRACE_PERIOD_DAYS)
            return timezone.now() <= grace_end
        return False

    def can_be_renewed(self):
        """Check if subscription can be renewed via TenantBillingPreferences"""
        if self.status not in ['active', 'expired']:
            return False

        # Check TenantBillingPreferences for renewal settings
        billing_prefs = self.tenant_billing_preferences
        if billing_prefs and billing_prefs.auto_renew_enabled and billing_prefs.renewal_status == 'active':
            return True

        return False

    def get_remaining_days(self):
        if self.status == 'active':
            remaining = self.end_date - timezone.now()
            return max(0, remaining.days)
        elif self.status == 'trial':
            if self.trial_end_date:
                remaining = self.trial_end_date - timezone.now()
                return max(0, remaining.days)
            return 0
        else:
            return 0

    @property
    def tenant_billing_preferences(self):
        """Get tenant's billing preferences (cached property for performance)"""
        try:
            return TenantBillingPreferences.objects.get(tenant_id=self.tenant_id)
        except TenantBillingPreferences.DoesNotExist:
            return None

    def __str__(self):
        return f"Subscription for Tenant {self.tenant_id} - {self.plan.name}"


class SubscriptionCredit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name='credits')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=100)  # e.g., 'proration', 'refund'
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    used = models.BooleanField(default=False)

    def __str__(self):
        return f"Credit {self.amount} for Subscription {self.subscription.id}"


class TrialUsage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(null=True, blank=True, help_text="Tenant identifier (nullable for machine-only tracking)")
    user_email = models.EmailField()
    machine_number = models.CharField(max_length=255, null=True, blank=True, db_index=True, help_text="Machine identifier to prevent multiple trial usage from one machine")
    trial_start_date = models.DateTimeField(default=timezone.now)
    trial_end_date = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True, help_text="IP address when trial was created")

    class Meta:
        # Machine number must be unique when provided to prevent multiple trials from same machine
        # Only enforce uniqueness when machine_number is not null
        constraints = [
            models.UniqueConstraint(
                fields=['machine_number'],
                name='unique_machine_trial',
                condition=models.Q(machine_number__isnull=False)
            ),
        ]
        indexes = [
            models.Index(fields=['machine_number']),
            models.Index(fields=['tenant_id', 'machine_number']),
        ]

    def __str__(self):
        machine_info = f" - Machine: {self.machine_number}" if self.machine_number else ""
        return f"Trial for Tenant {self.tenant_id} - {self.user_email}{machine_info}"


class TenantBillingPreferences(models.Model):
    """
    COMPLETE TENANT BILLING MANAGEMENT: Consolidated billing preferences, payment methods, and renewal logic.
    One record per tenant - no duplicates, just update existing record.
    Handles all auto-renewal, payment method storage, and billing preferences.
    """
    PROVIDER_CHOICES = (
        ('paystack', 'Paystack'),
        ('flutterwave', 'Flutterwave'),
    )

    STATUS_CHOICES = (
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('canceled', 'Canceled'),
        ('processing', 'Processing'),
        ('failed', 'Failed'),
    )

    tenant_id = models.UUIDField(primary_key=True, help_text="Tenant identifier - unique per tenant")
    user_id = models.CharField(max_length=255, null=True, blank=True, help_text="User who manages billing preferences")

    # Auto-renewal settings (consolidated from AutoRenewal model)
    auto_renew_enabled = models.BooleanField(default=True, help_text="Whether auto-renewal is enabled")
    renewal_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active',
                                    help_text="Current renewal status")
    preferred_plan = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, blank=True,
                                     help_text="Plan to renew to (defaults to current plan)")
    subscription_expiry_date = models.DateTimeField(null=True, blank=True,
                                                  help_text="Date when current subscription expires")
    next_renewal_date = models.DateTimeField(null=True, blank=True, help_text="Next scheduled renewal date")
    renewal_failure_count = models.IntegerField(default=0, help_text="Consecutive renewal failures")
    max_renewal_failures = models.IntegerField(default=3, help_text="Max failures before auto-disable")
    last_renewal_attempt = models.DateTimeField(null=True, blank=True, help_text="Last renewal attempt timestamp")

    # Payment method details (consolidated from RecurringToken model)
    payment_provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES, null=True, blank=True)
    paystack_subscription_code = models.CharField(max_length=255, null=True, blank=True,
                                                help_text="Paystack subscription code for recurring payments")
    paystack_authorization_code = models.CharField(max_length=255, null=True, blank=True,
                                                 help_text="Reusable authorization code for charging saved card (Paystack)")
    paystack_customer_code = models.CharField(max_length=255, null=True, blank=True,
                                            help_text="Paystack customer identifier")
    flutterwave_payment_method_id = models.CharField(max_length=255, null=True, blank=True,
                                                    help_text="Flutterwave payment method ID")
    flutterwave_customer_id = models.CharField(max_length=255, null=True, blank=True,
                                              help_text="Flutterwave customer ID")
    card_last4 = models.CharField(max_length=4, null=True, blank=True, help_text="Last 4 digits of card")
    card_brand = models.CharField(max_length=50, null=True, blank=True, help_text="Card brand (Visa, Mastercard, etc.)")
    payment_email = models.EmailField(null=True, blank=True, help_text="Email associated with payment method")

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_payment_at = models.DateTimeField(null=True, blank=True, help_text="Last successful payment timestamp")
    notes = models.TextField(blank=True, help_text="Additional billing notes")

    class Meta:
        indexes = [
            models.Index(fields=['auto_renew_enabled', 'next_renewal_date']),
            models.Index(fields=['tenant_id', 'auto_renew_enabled']),
            models.Index(fields=['renewal_status', 'next_renewal_date']),
            models.Index(fields=['payment_provider', 'paystack_subscription_code']),
            models.Index(fields=['subscription_expiry_date', 'renewal_status']),
        ]

    def __str__(self):
        return f"Billing Preferences for Tenant {self.tenant_id}"

    def is_due_for_renewal(self):
        """Check if tenant's subscription is due for renewal"""
        if not self.auto_renew_enabled or self.renewal_status != 'active':
            return False
        if not self.next_renewal_date:
            # If no next_renewal_date set, use subscription_expiry_date
            return timezone.now() >= (self.subscription_expiry_date or timezone.now())
        return timezone.now() >= self.next_renewal_date

    def can_renew(self):
        """Check if renewal can be processed"""
        if not self.auto_renew_enabled or self.renewal_status != 'active':
            return False, f"Auto-renewal is disabled or status is {self.renewal_status}"

        if self.renewal_failure_count >= self.max_renewal_failures:
            return False, f"Maximum failure count ({self.max_renewal_failures}) reached"

        if not self.payment_provider:
            return False, "No payment method configured"

        if not self.preferred_plan:
            return False, "No preferred plan set for renewal"

        if self.preferred_plan.discontinued or not self.preferred_plan.is_active:
            return False, "Preferred plan is not available"

        return True, "OK"

    def record_renewal_success(self):
        """Update after successful renewal"""
        self.last_payment_at = timezone.now()
        self.last_renewal_attempt = timezone.now()
        self.renewal_failure_count = 0
        self.renewal_status = 'active'
        # next_renewal_date will be set by renewal logic
        self.save()

    def record_renewal_failure(self):
        """Update after failed renewal"""
        self.renewal_failure_count += 1
        self.last_renewal_attempt = timezone.now()
        if self.renewal_failure_count >= self.max_renewal_failures:
            self.renewal_status = 'failed'
            self.auto_renew_enabled = False  # Auto-disable after max failures
        self.save()

    def update_payment_method(self, provider, **payment_data):
        """Update payment method details"""
        self.payment_provider = provider
        if provider == 'paystack':
            self.paystack_subscription_code = payment_data.get('subscription_code')
        elif provider == 'flutterwave':
            self.flutterwave_payment_method_id = payment_data.get('payment_method_id')
            self.flutterwave_customer_id = payment_data.get('customer_id')

        self.card_last4 = payment_data.get('last4')
        self.card_brand = payment_data.get('card_brand')
        self.payment_email = payment_data.get('email')
        self.save()

    def get_payment_method_info(self):
        """Get formatted payment method information"""
        if not self.payment_provider:
            return None

        return {
            'provider': self.payment_provider,
            'card_last4': self.card_last4,
            'card_brand': self.card_brand,
            'email': self.payment_email,
            'paystack_subscription_code': self.paystack_subscription_code,
            'flutterwave_payment_method_id': self.flutterwave_payment_method_id,
            'flutterwave_customer_id': self.flutterwave_customer_id,
        }