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
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey('Subscription', on_delete=models.CASCADE, related_name='audit_logs')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
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

    def clean(self):
        if self.max_users <= 0:
            raise ValidationError("Max users must be greater than 0")
        if self.max_branches <= 0:
            raise ValidationError("Max branches must be greater than 0")
        if self.price < 0:
            raise ValidationError("Price cannot be negative")
        if self.duration_days <= 0:
            raise ValidationError("Duration must be greater than 0")

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
    tenant_id = models.UUIDField(unique=True, default=uuid.uuid4)
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE)
    scheduled_plan = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, blank=True, related_name='scheduled_subscriptions')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='trial')
    start_date = models.DateTimeField(default=timezone.now)
    end_date = models.DateTimeField()
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
        return self.status in ['active', 'expired'] and self.auto_renew

    def get_remaining_days(self):
        if self.status != 'active':
            return 0
        remaining = self.end_date - timezone.now()
        return max(0, remaining.days)

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
    tenant_id = models.UUIDField(unique=True)
    user_email = models.EmailField()
    trial_start_date = models.DateTimeField(default=timezone.now)
    trial_end_date = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Trial for Tenant {self.tenant_id} - {self.user_email}"