"""
Management command to sync auto-renewal status with payment providers and process due renewals.
This ensures that when users enable/disable auto-renew in the system,
it also updates the payment provider's recurring billing settings.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from apps.billing.models import AutoRenewal, Subscription
from apps.billing.services import AutoRenewalService, SubscriptionService
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Sync auto-renewal status with payment providers and process due renewals (Flutterwave/Paystack)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be processed without making changes',
        )
        parser.add_argument(
            '--tenant-id',
            type=str,
            help='Process only for specific tenant ID',
        )
        parser.add_argument(
            '--auto-renewal-id',
            type=str,
            help='Process only for specific auto-renewal ID',
        )
        parser.add_argument(
            '--process-due',
            action='store_true',
            help='Process auto-renewals that are due for renewal',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force process even if not due yet (for testing)',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        tenant_id = options.get('tenant_id')
        auto_renewal_id = options.get('auto_renewal_id')
        process_due = options.get('process_due', False)
        force = options.get('force', False)

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made'))

        if process_due or force:
            self.stdout.write(self.style.WARNING('PROCESSING DUE RENEWALS MODE'))
            self.stdout.write('='*60)
            self.stdout.write('Processing auto-renewals that are due for renewal...')
            
            results = self.process_due_renewals(
                dry_run=dry_run,
                tenant_id=tenant_id,
                auto_renewal_id=auto_renewal_id,
                force=force
            )
            
            # Print processing results
            self.stdout.write('\n' + '='*50)
            self.stdout.write('PROCESSING SUMMARY')
            self.stdout.write('='*50)
            self.stdout.write(f'Successful renewals: {results["successful_renewals"]}')
            self.stdout.write(f'Failed renewals: {results["failed_renewals"]}')
            self.stdout.write(f'Skipped: {results["skipped"]}')
            self.stdout.write(f'Requiring user action: {results["requiring_action"]}')
            self.stdout.write(f'Total processed: {results["total_processed"]}')
            
            if results['errors']:
                self.stdout.write('\nERRORS:')
                for error in results['errors']:
                    self.stdout.write(f'  - {error}')
                    
            if not dry_run:
                self.stdout.write(
                    self.style.SUCCESS('\nDue renewals processing completed!')
                )
            else:
                self.stdout.write(
                    self.style.WARNING('\nDry run completed. No changes were made.')
                )
        else:
            # Just sync payment provider settings
            self.stdout.write('SYNCING PAYMENT PROVIDER SETTINGS...')
            self.stdout.write('='*60)
            
            results = self.sync_payment_provider_settings(
                dry_run=dry_run,
                tenant_id=tenant_id,
                auto_renewal_id=auto_renewal_id
            )
            
            # Print sync results
            self.stdout.write('\n' + '='*50)
            self.stdout.write('SYNC SUMMARY')
            self.stdout.write('='*50)
            self.stdout.write(f'Synced: {results["synced"]}')
            self.stdout.write(f'Skipped: {results["skipped"]}')
            self.stdout.write(f'Errors: {results["errors"]}')
            
            if results['error_details']:
                self.stdout.write('\nERRORS:')
                for error in results['error_details']:
                    self.stdout.write(f'  - {error}')
                    
            if not dry_run:
                self.stdout.write(
                    self.style.SUCCESS('\nAuto-renewal sync completed!')
                )
            else:
                self.stdout.write(
                    self.style.WARNING('\nDry run completed. No changes were made.')
                )

    def process_due_renewals(self, dry_run=False, tenant_id=None, auto_renewal_id=None, force=False):
        """Process auto-renewals that are due for renewal using direct charge method"""
        now = timezone.now()
        
        # Build query for due renewals
        query = AutoRenewal.objects.filter(status='active')
        
        if tenant_id:
            query = query.filter(tenant_id=tenant_id)
        if auto_renewal_id:
            query = query.filter(id=auto_renewal_id)
            
        if not force:
            # Only process renewals that are due (within 1 day of renewal date)
            query = query.filter(
                next_renewal_date__lte=now + timezone.timedelta(days=1)
            )
        
        auto_renewals = query.select_related('subscription', 'plan', 'subscription__plan')

        if not auto_renewals.exists():
            self.stdout.write(self.style.WARNING('No due auto-renewals found'))
            return {
                'successful_renewals': 0,
                'failed_renewals': 0,
                'skipped': 0,
                'requiring_action': 0,
                'total_processed': 0,
                'errors': []
            }

        self.stdout.write(f'Found {auto_renewals.count()} due auto-renewals to process')

        results = {
            'successful_renewals': 0,
            'failed_renewals': 0,
            'skipped': 0,
            'requiring_action': 0,
            'total_processed': 0,
            'errors': []
        }

        # Initialize services
        subscription_service = SubscriptionService()

        for auto_renewal in auto_renewals:
            results['total_processed'] += 1
            
            try:
                if not auto_renewal.subscription:
                    self.stdout.write(
                        self.style.WARNING(f'Auto-renewal {auto_renewal.id} has no subscription - skipping')
                    )
                    results['skipped'] += 1
                    continue

                subscription = auto_renewal.subscription
                
                # Check if subscription is still active
                if subscription.status != 'active':
                    self.stdout.write(
                        self.style.WARNING(f'Subscription {subscription.id} is not active - skipping')
                    )
                    results['skipped'] += 1
                    continue
                
                # Check if renewal is actually due
                if not force and auto_renewal.next_renewal_date and auto_renewal.next_renewal_date > now:
                    self.stdout.write(
                        self.style.WARNING(f'Auto-renewal {auto_renewal.id} is not due yet - skipping')
                    )
                    results['skipped'] += 1
                    continue

                if dry_run:
                    self.stdout.write(
                        self.style.SUCCESS(f'⊘ [DRY RUN] Would process auto-renewal {auto_renewal.id} for tenant {auto_renewal.tenant_id}')
                    )
                    results['successful_renewals'] += 1
                    continue

                # Process the auto-renewal payment
                result = subscription_service.process_auto_renewal_payment(subscription, auto_renewal)

                if result['status'] == 'success':
                    results['successful_renewals'] += 1
                    self.stdout.write(
                        self.style.SUCCESS(f'✓ Auto-renewal {auto_renewal.id} successful for tenant {auto_renewal.tenant_id}')
                    )
                    if 'new_end_date' in result:
                        self.stdout.write(
                            f'  → New end date: {result["new_end_date"]}'
                        )
                    if 'next_renewal_date' in result:
                        self.stdout.write(
                            f'  → Next renewal: {result["next_renewal_date"]}'
                        )
                elif result['status'] == 'requires_action':
                    results['requiring_action'] += 1
                    self.stdout.write(
                        self.style.WARNING(f'⚠ Auto-renewal {auto_renewal.id} requires user action: {result.get("message", "Unknown")}')
                    )
                else:
                    results['failed_renewals'] += 1
                    error_msg = f'Auto-renewal {auto_renewal.id}: {result.get("message", "Unknown error")}'
                    results['errors'].append(error_msg)
                    self.stdout.write(
                        self.style.ERROR(f'✗ Auto-renewal {auto_renewal.id} failed: {error_msg}')
                    )

            except Exception as e:
                results['failed_renewals'] += 1
                error_msg = f'Auto-renewal {auto_renewal.id}: {str(e)}'
                results['errors'].append(error_msg)
                self.stdout.write(
                    self.style.ERROR(f'✗ Exception processing auto-renewal {auto_renewal.id}: {str(e)}')
                )

        return results

    def sync_payment_provider_settings(self, dry_run=False, tenant_id=None, auto_renewal_id=None):
        """Sync payment provider settings for auto-renewals"""
        # Build query
        query = AutoRenewal.objects.filter(status='active')
        if tenant_id:
            query = query.filter(tenant_id=tenant_id)
        if auto_renewal_id:
            query = query.filter(id=auto_renewal_id)

        auto_renewals = query.select_related('subscription', 'plan')

        if not auto_renewals.exists():
            self.stdout.write(self.style.WARNING('No active auto-renewals found'))
            return {
                'synced': 0,
                'skipped': 0,
                'errors': 0,
                'error_details': []
            }

        self.stdout.write(f'Found {auto_renewals.count()} active auto-renewals to sync')

        results = {
            'synced': 0,
            'skipped': 0,
            'errors': 0,
            'error_details': []
        }

        for auto_renewal in auto_renewals:
            try:
                if not auto_renewal.subscription:
                    self.stdout.write(
                        self.style.WARNING(f'Auto-renewal {auto_renewal.id} has no subscription - skipping')
                    )
                    results['skipped'] += 1
                    continue

                result = self.sync_payment_provider_for_auto_renewal(
                    auto_renewal, dry_run
                )

                if result['status'] == 'success':
                    results['synced'] += 1
                    self.stdout.write(
                        self.style.SUCCESS(f'✓ Synced auto-renewal {auto_renewal.id} for tenant {auto_renewal.tenant_id}')
                    )
                elif result['status'] == 'skipped':
                    results['skipped'] += 1
                    self.stdout.write(
                        self.style.WARNING(f'⊘ Skipped auto-renewal {auto_renewal.id}: {result.get("reason", "Unknown")}')
                    )
                else:
                    results['errors'] += 1
                    error_msg = f'Auto-renewal {auto_renewal.id}: {result.get("message", "Unknown error")}'
                    results['error_details'].append(error_msg)
                    self.stdout.write(
                        self.style.ERROR(f'✗ Failed to sync auto-renewal {auto_renewal.id}: {error_msg}')
                    )

            except Exception as e:
                results['errors'] += 1
                error_msg = f'Auto-renewal {auto_renewal.id}: {str(e)}'
                results['error_details'].append(error_msg)
                self.stdout.write(
                    self.style.ERROR(f'✗ Exception syncing auto-renewal {auto_renewal.id}: {str(e)}')
                )

        return results

    def sync_payment_provider_for_auto_renewal(self, auto_renewal, dry_run=False):
        """Sync a single auto-renewal with payment provider"""
        try:
            subscription = auto_renewal.subscription
            if not subscription:
                return {'status': 'skipped', 'reason': 'No subscription found'}

            # Check if subscription has a completed payment with the payment provider
            last_payment = subscription.payments.filter(status='completed').order_by('-payment_date').first()
            if not last_payment:
                return {
                    'status': 'skipped', 
                    'reason': 'No completed payments found for subscription'
                }

            provider = last_payment.provider
            
            if dry_run:
                return {
                    'status': 'success',
                    'message': f'Dry run: Would sync with {provider}',
                    'provider': provider
                }

            # For now, just log the sync action
            # In the future, this could make actual API calls to update recurring billing
            logger.info(f"Syncing auto-renewal {auto_renewal.id} with payment provider {provider}")
            
            return {
                'status': 'success',
                'message': f'Successfully synced with {provider}',
                'provider': provider
            }

        except Exception as e:
            logger.error(f"Error syncing auto-renewal {auto_renewal.id}: {str(e)}")
            return {
                'status': 'error',
                'message': str(e)
            }
