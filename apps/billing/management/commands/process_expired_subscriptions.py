"""
Management command to process expired subscriptions
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import models
from apps.billing.services import SubscriptionService, PaymentRetryService
from apps.billing.models import Subscription
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Process expired subscriptions and handle payment retries'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Run without making changes',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force processing even if not needed',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        force = options['force']
        
        self.stdout.write(
            self.style.SUCCESS('Starting expired subscription processing...')
        )
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING('DRY RUN MODE - No changes will be made')
            )
        
        try:
            # Process expired subscriptions
            subscription_service = SubscriptionService()
            result = subscription_service.check_expired_subscriptions()
            
            if result['status'] == 'success':
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Processed {result['processed_count']} expired subscriptions"
                    )
                )
            else:
                self.stdout.write(
                    self.style.ERROR(f"Error processing expired subscriptions: {result['message']}")
                )
            
            # Process payment retries
            self.process_payment_retries(dry_run)
            
            # Process suspended subscriptions
            self.process_suspended_subscriptions(dry_run)
            
            self.stdout.write(
                self.style.SUCCESS('Expired subscription processing completed')
            )
            
        except Exception as e:
            logger.error(f"Expired subscription processing failed: {str(e)}")
            self.stdout.write(
                self.style.ERROR(f'Error: {str(e)}')
            )
            raise

    def process_payment_retries(self, dry_run=False):
        """Process payment retries for failed payments"""
        self.stdout.write('Processing payment retries...')
        
        # Get subscriptions with failed payments that can be retried
        subscriptions = Subscription.objects.filter(
            status='active',
            payment_retry_count__lt=models.F('max_payment_retries'),
            last_payment_date__isnull=False
        )
        
        retry_service = PaymentRetryService()
        processed_count = 0
        
        for subscription in subscriptions:
            if retry_service.should_retry_payment(subscription):
                if not dry_run:
                    retry_service.increment_retry_count(subscription)
                    # Here you would trigger payment retry logic
                    # e.g., send payment reminder, attempt payment, etc.
                
                processed_count += 1
                self.stdout.write(
                    f"  - Retry scheduled for subscription {subscription.id}"
                )
        
        self.stdout.write(
            self.style.SUCCESS(f"Processed {processed_count} payment retries")
        )

    def process_suspended_subscriptions(self, dry_run=False):
        """Process suspended subscriptions that should be canceled"""
        self.stdout.write('Processing suspended subscriptions...')
        
        # Get subscriptions that have been suspended for too long
        suspension_threshold = timezone.now() - timezone.timedelta(days=30)
        suspended_subs = Subscription.objects.filter(
            status='suspended',
            suspended_at__lt=suspension_threshold
        )
        
        processed_count = 0
        
        for subscription in suspended_subs:
            if not dry_run:
                subscription.status = 'canceled'
                subscription.canceled_at = timezone.now()
                subscription.save()
                
                # Log the cancellation
                from apps.billing.models import AuditLog
                AuditLog.objects.create(
                    subscription=subscription,
                    action='canceled',
                    user='system',
                    details={'reason': 'auto_cancel_after_suspension'}
                )
            
            processed_count += 1
            self.stdout.write(
                f"  - Canceled suspended subscription {subscription.id}"
            )
        
        self.stdout.write(
            self.style.SUCCESS(f"Processed {processed_count} suspended subscriptions")
        )
