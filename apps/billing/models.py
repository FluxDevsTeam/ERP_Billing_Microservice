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
        ("Finance", "Finance"),
        ("Healthcare", "Healthcare"),
        ("Production", "Production"),
        ("Education", "Education"),
        ("Technology", "Technology"),
        ("Retail", "Retail"),
        ("Hospitality", "Hospitality"),
        ("Agriculture", "Agriculture"),
        ("Transport and Logistics", "Transport and Logistics"),
        ("Real Estate", "Real Estate"),
        ("Energy and Utilities", "Energy and Utilities"),
        ("Media and Entertainment", "Media and Entertainment"),
        ("Government", "Government"),
        ("Other", "Other"),
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
    auto_renew = models.BooleanField(default=True)
    trial_end_date = models.DateTimeField(null=True, blank=True)
    last_payment_date = models.DateTimeField(null=True, blank=True)
    next_payment_date = models.DateTimeField(null=True, blank=True)
    payment_retry_count = models.IntegerField(default=0)
    max_payment_retries = models.IntegerField(default=3)
    is_first_time_subscription = models.BooleanField(default=True)
    trial_used = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=['tenant_id', 'status']),
            models.Index(fields=['status', 'end_date']),
            models.Index(fields=['tenant_id', 'end_date']),
            models.Index(fields=['auto_renew', 'status']),
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
        """Check if subscription can be renewed (either via auto_renew field for backwards compatibility or via AutoRenewal)"""
        if self.status not in ['active', 'expired']:
            return False
        
        # Check if there's an active AutoRenewal
        active_auto_renewal = self.auto_renewals.filter(status='active').first()
        if active_auto_renewal:
            return True
        
        # Fallback to auto_renew field for backwards compatibility
        return self.auto_renew

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

    def __str__(self):
        return f"Subscription for Tenant {self.tenant_id} - {self.plan.name}"


class AutoRenewal(models.Model):
    """Model to handle auto-renewal of subscriptions"""
    STATUS_CHOICES = (
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('canceled', 'Canceled'),
        ('processing', 'Processing'),
        ('failed', 'Failed'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(help_text="Tenant identifier for the subscription", db_index=True)
    user_id = models.CharField(max_length=255, null=True, blank=True, help_text="User identifier who set up the auto-renewal")
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name='auto_renewals', null=True, blank=True, help_text="Optional reference to subscription")
    plan = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, related_name='auto_renewals', help_text="Plan to renew to")
    expiry_date = models.DateTimeField(help_text="Date when the current subscription period expires", db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_renewal_at = models.DateTimeField(null=True, blank=True, help_text="Last successful renewal timestamp")
    next_renewal_date = models.DateTimeField(null=True, blank=True, help_text="Next scheduled renewal date", db_index=True)
    failure_count = models.IntegerField(default=0, help_text="Number of consecutive renewal failures")
    max_failures = models.IntegerField(default=3, help_text="Maximum allowed failures before auto-pause")
    notes = models.TextField(blank=True, help_text="Additional notes about the auto-renewal")

    class Meta:
        unique_together = [('tenant_id', 'plan')]
        ordering = ['expiry_date']
        indexes = [
            models.Index(fields=['tenant_id', 'status']),
            models.Index(fields=['expiry_date', 'status']),
            models.Index(fields=['status', 'next_renewal_date']),
            models.Index(fields=['tenant_id', 'expiry_date']),
        ]

    def __str__(self):
        return f"AutoRenewal for Tenant {self.tenant_id} - {self.plan.name if self.plan else 'No Plan'}"

    def is_due_for_renewal(self):
        """Check if the auto-renewal is due"""
        if self.status != 'active':
            return False
        if not self.next_renewal_date:
            # If no next_renewal_date set, use expiry_date
            return timezone.now() >= self.expiry_date
        return timezone.now() >= self.next_renewal_date

    def can_renew(self):
        """Check if the auto-renewal can be processed"""
        if self.status != 'active':
            return False, f"Auto-renewal status is {self.status}"
        if self.failure_count >= self.max_failures:
            return False, f"Maximum failure count ({self.max_failures}) reached"
        if not self.plan:
            return False, "No plan associated with auto-renewal"
        if self.plan.discontinued:
            return False, "Plan is discontinued"
        if not self.plan.is_active:
            return False, "Plan is not active"
        return True, "OK"


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


class RecurringToken(models.Model):
    """Model to store recurring payment tokens for subscriptions"""
    PROVIDER_CHOICES = (
        ('paystack', 'Paystack'),
        ('flutterwave', 'Flutterwave'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.OneToOneField(Subscription, on_delete=models.CASCADE, related_name='recurring_token')
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    paystack_subscription_code = models.CharField(max_length=255, null=True, blank=True, help_text="Paystack subscription code for managing recurring payments")
    last4 = models.CharField(max_length=4, null=True, blank=True, help_text="Last 4 digits of card")
    card_brand = models.CharField(max_length=50, null=True, blank=True, help_text="Card brand (Visa, Mastercard, etc.)")
    email = models.EmailField(null=True, blank=True, help_text="Email associated with payment method")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['provider', 'is_active']),
            models.Index(fields=['subscription', 'is_active']),
        ]

    def __str__(self):
        return f"{self.provider} token for {self.subscription.tenant_id} - {self.last4 or 'N/A'}"