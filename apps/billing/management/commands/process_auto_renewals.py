"""
Management command to process auto-renewals
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.billing.services import AutoRenewalService
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Process due auto-renewals'

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
            self.style.SUCCESS('Starting auto-renewal processing...')
        )
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING('DRY RUN MODE - No changes will be made')
            )
            # In dry run mode, just show what would be processed
            from apps.billing.models import AutoRenewal
            due_renewals = AutoRenewal.objects.filter(
                status='active',
                next_renewal_date__lte=timezone.now()
            ).select_related('plan', 'subscription')
            
            self.stdout.write(f"Would process {due_renewals.count()} auto-renewals:")
            for renewal in due_renewals:
                self.stdout.write(
                    f"  - Tenant {renewal.tenant_id} - Plan {renewal.plan.name if renewal.plan else 'No Plan'} - "
                    f"Next renewal: {renewal.next_renewal_date}"
                )
            return
        
        try:
            auto_renewal_service = AutoRenewalService()
            result = auto_renewal_service.process_due_auto_renewals()
            
            if result['status'] == 'success':
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Processed {result['processed']} auto-renewals: "
                        f"{result['succeeded']} succeeded, "
                        f"{result['failed']} failed, "
                        f"{result['skipped']} skipped"
                    )
                )
            else:
                self.stdout.write(
                    self.style.ERROR(f"Error processing auto-renewals: {result['message']}")
                )
            
            self.stdout.write(
                self.style.SUCCESS('Auto-renewal processing completed')
            )
            
        except Exception as e:
            logger.error(f"Auto-renewal processing failed: {str(e)}")
            self.stdout.write(
                self.style.ERROR(f'Error: {str(e)}')
            )
            raise

