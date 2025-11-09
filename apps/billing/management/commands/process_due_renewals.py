"""
Management command to process due auto-renewals using the direct charge method.
This can be run by a cron job to handle automatic renewals.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.billing.models import AutoRenewal
from apps.billing.services import AutoRenewalService
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Process all due auto-renewals using direct charge method'

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
            '--force',
            action='store_true',
            help='Force process even if not due yet (for testing)',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        tenant_id = options.get('tenant_id')
        auto_renewal_id = options.get('auto_renewal_id')
        force = options.get('force', False)

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made'))

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
            return

        self.stdout.write(f'Found {auto_renewals.count()} due auto-renewals to process')

        results = {
            'successful_renewals': 0,
            'failed_renewals': 0,
            'skipped': 0,
            'requiring_action': 0,
            'total_processed': 0,
            'errors': []
        }

        # Initialize service
        auto_renewal_service = AutoRenewalService()

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

                # Process the auto-renewal using the new direct charge method
                result = auto_renewal_service.process_auto_renewal(str(auto_renewal.id))

                if result['status'] == 'success':
                    results['successful_renewals'] += 1
                    self.stdout.write(
                        self.style.SUCCESS(f'✓ Auto-renewal {auto_renewal.id} successful for tenant {auto_renewal.tenant_id}')
                    )
                    if 'new_end_date' in result.get('payment_details', {}):
                        self.stdout.write(
                            f'  → New end date: {result["payment_details"]["new_end_date"]}'
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

        # Print summary
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