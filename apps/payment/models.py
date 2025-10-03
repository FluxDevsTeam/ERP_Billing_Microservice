from django.db import models
from django.utils import timezone
from apps.billing.models import Subscription, Plan
import uuid


class Payment(models.Model):
    PAYMENT_TYPE_CHOICES = (
        ('initial', 'Initial'),
        ('renewal', 'Renewal'),
        ('upgrade', 'Upgrade'),
        ('advance', 'Advance'),
    )
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    )
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name='payments')
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, null=True, blank=True, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_date = models.DateTimeField(default=timezone.now)
    transaction_id = models.CharField(max_length=128, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    provider = models.CharField(max_length=50)
    payment_type = models.CharField(max_length=20, choices=PAYMENT_TYPE_CHOICES, default='initial')

    def __str__(self):
        return f"Payment {self.transaction_id} for Plan {self.plan.id}"


class WebhookEvent(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('processed', 'Processed'),
        ('failed', 'Failed'),
    )
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=50)
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    retry_count = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=3)
    last_retry_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    error_message = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"Webhook {self.id} - {self.provider} - {self.status}"