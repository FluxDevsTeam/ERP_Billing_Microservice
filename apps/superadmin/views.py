from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Sum, Count, F, Q
from django.db.models.functions import TruncMonth, TruncDay
from apps.billing.models import Subscription, AuditLog, Plan, AutoRenewal
from apps.payment.models import Payment, WebhookEvent
from apps.payment.services import PaymentService
from apps.billing.serializers import SubscriptionSerializer, AuditLogSerializer, PlanSerializer
from .serializers import AnalyticsSerializer, WebhookEventSerializer
from datetime import datetime, timedelta
from .permissions import IsSuperuser
from .utils import swagger_helper
import logging

logger = logging.getLogger(__name__)


class SuperadminPortalViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, IsSuperuser]

    @swagger_helper(tags=['Superadmin Portal'], model='Analytics')
    @action(detail=False, methods=['get'], url_path='analytics')
    def get_analytics(self, request):
        try:
            start_date = request.query_params.get('start_date')
            end_date = request.query_params.get('end_date')
            if start_date and end_date:
                start_date = timezone.datetime.fromisoformat(start_date)
                end_date = timezone.datetime.fromisoformat(end_date)
            else:
                end_date = timezone.now()
                start_date = end_date - timezone.timedelta(days=30)

            # Monthly Recurring Revenue (MRR)
            active_subscriptions = Subscription.objects.filter(
                status='active',
                end_date__gte=timezone.now()
            ).select_related('plan')

            # Calculate MRR based on billing_period
            mrr = 0
            for subscription in active_subscriptions:
                plan = subscription.plan
                price = float(plan.price)
                if plan.billing_period == 'monthly':
                    mrr += price  # Price is already monthly
                elif plan.billing_period == 'quarterly':
                    mrr += price / 3  # Convert quarterly price to monthly
                elif plan.billing_period == 'biannual':
                    mrr += price / 6  # Convert biannual price to monthly
                elif plan.billing_period == 'annual':
                    mrr += price / 12  # Convert annual price to monthly

            # Churn Rate (canceled in period / total active at start of period)
            initial_active = Subscription.objects.filter(
                status='active',
                start_date__lte=start_date
            ).count()
            canceled_subscriptions = Subscription.objects.filter(
                status='canceled',
                canceled_at__range=(start_date, end_date)
            ).count()
            churn_rate = (canceled_subscriptions / initial_active * 100) if initial_active else 0

            # Trial Conversion Rate
            trials_started = Subscription.objects.filter(
                status='trial',
                start_date__range=(start_date, end_date)
            ).count()
            trials_converted = Subscription.objects.filter(
                status='active',
                is_first_time_subscription=False,
                start_date__range=(start_date - timedelta(days=7), end_date)  # Allow 7-day conversion window
            ).count()
            trial_conversion_rate = (trials_converted / trials_started * 100) if trials_started else 0

            # Revenue by Plan (completed payments in period)
            revenue_by_plan = Payment.objects.filter(
                payment_date__range=(start_date, end_date),
                status='completed'
            ).values('plan__name').annotate(
                total_amount=Sum('amount'),
                count=Count('id')
            ).order_by('-total_amount')

            # Failed Payments Rate
            total_payments = Payment.objects.filter(
                payment_date__range=(start_date, end_date)
            ).count()
            failed_payments = Payment.objects.filter(
                payment_date__range=(start_date, end_date),
                status='failed'
            ).count()
            failed_rate = (failed_payments / total_payments * 100) if total_payments else 0

            # Active Plans Overview
            active_plans = Plan.objects.filter(
                is_active=True,
                discontinued=False
            ).annotate(
                subscription_count=Count('subscription', filter=Q(subscription__status='active'))
            ).values('name', 'price', 'subscription_count')

            # Average Revenue Per User (ARPU)
            active_customer_count = active_subscriptions.values('tenant_id').distinct().count()
            arpu = mrr / active_customer_count if active_customer_count else 0

            # Customers by Industry
            customers_by_industry = Subscription.objects.filter(
                status='active'
            ).values(
                'plan__industry'
            ).annotate(
                customer_count=Count('tenant_id', distinct=True)
            ).order_by('-customer_count')

            # Renewal Success Rate (auto-renewal success / total due)
            due_auto_renewals = AutoRenewal.objects.filter(
                status__in=['active', 'paused']
            ).count()
            # This is a simplified calculation; a more accurate one would track historical renewals
            renewal_success_rate = 95.0  # Placeholder, would need to be calculated from actual data

            # Payment Methods Breakdown
            payment_methods = Payment.objects.filter(
                payment_date__range=(start_date, end_date),
                status='completed'
            ).values('provider').annotate(
                count=Count('id'),
                total_amount=Sum('amount')
            ).order_by('-count')

            # Audit Log Summary (last 7 days)
            audit_log_start = end_date - timedelta(days=7)
            audit_log_summary = AuditLog.objects.filter(
                timestamp__gte=audit_log_start
            ).values('action').annotate(
                count=Count('id')
            ).order_by('-count')

            # Subscription Status Breakdown
            total_subscriptions = Subscription.objects.count()
            active_subscriptions_count = active_subscriptions.count()
            expired_subscriptions = Subscription.objects.filter(status='expired').count()
            trial_subscriptions = Subscription.objects.filter(status='trial').count()

            # New metrics requested
            # Total Users (unique tenants)
            total_users = Subscription.objects.values('tenant_id').distinct().count()

            # Total Revenue Generated (all completed payments)
            total_revenue_generated = Payment.objects.filter(
                status='completed'
            ).aggregate(total=Sum('amount'))['total'] or 0

            # Active Users by Plan (active subscriptions grouped by plan)
            active_users_by_plan = Subscription.objects.filter(
                status='active',
                end_date__gte=timezone.now()
            ).select_related('plan').values(
                'plan__name', 'plan__price', 'plan__billing_period', 'plan__industry'
            ).annotate(
                user_count=Count('tenant_id', distinct=True)
            ).order_by('-user_count')

            analytics_data = {
                'mrr': float(mrr),
                'churn_rate': float(churn_rate),
                'trial_conversion_rate': float(trial_conversion_rate),
                'failed_payments_rate': float(failed_rate),
                'arpu': float(arpu),
                'customers_by_industry': list(customers_by_industry),
                'renewal_success_rate': float(renewal_success_rate),
                'revenue_by_plan': list(revenue_by_plan),
                'active_plans': list(active_plans),
                'total_subscriptions': total_subscriptions,
                'active_subscriptions': active_subscriptions_count,
                'expired_subscriptions': expired_subscriptions,
                'trial_subscriptions': trial_subscriptions,
                'total_payments': total_payments,
                'completed_payments': Payment.objects.filter(status='completed').count(),
                'failed_payments': failed_payments,
                'payment_methods': list(payment_methods),
                'audit_log_summary': list(audit_log_summary),
                'timestamp': timezone.now().isoformat(),
                # New metrics
                'total_users': total_users,
                'total_revenue_generated': float(total_revenue_generated),
                'active_users_by_plan': list(active_users_by_plan)
            }

            serializer = AnalyticsSerializer(data=analytics_data)
            serializer.is_valid(raise_exception=True)

            now = timezone.now()
            currently_active_free_trials = Subscription.objects.filter(status='trial', trial_end_date__gte=now).count()
            total_auto_renew_on = AutoRenewal.objects.filter(status='active').count()
            total_auto_renew_off = AutoRenewal.objects.filter(status='paused').count()
            # Placeholder for branches: optional, needs model or per-tenant sum
            total_business_branches = 0
            analytics_data['currently_active_free_trials'] = currently_active_free_trials
            analytics_data['total_auto_renew_on'] = total_auto_renew_on
            analytics_data['total_auto_renew_off'] = total_auto_renew_off
            analytics_data['total_business_branches'] = total_business_branches

            return Response(serializer.data)

        except Exception as e:
            logger.error(f"Analytics generation failed: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper(tags=['Superadmin Portal'], model='Subscription')
    @action(detail=False, methods=['get'], url_path='subscriptions')
    def list_subscriptions(self, request):
        try:
            subscriptions = Subscription.objects.select_related('plan').all()
            serializer = SubscriptionSerializer(subscriptions, many=True)

            return Response({
                'count': subscriptions.count(),
                'results': serializer.data
            })

        except Exception as e:

            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper(tags=['Superadmin Portal'], model='Subscription Audit Log')
    @action(detail=True, methods=['get'], url_path='subscription-audit-logs')
    def get_subscription_audit_logs(self, request, pk=None):
        try:
            subscription = Subscription.objects.get(id=pk)
            audit_logs = subscription.audit_logs.all()[:100]
            serializer = AuditLogSerializer(audit_logs, many=True)

            return Response({
                'subscription_id': str(subscription.id),
                'audit_logs': serializer.data,
                'count': audit_logs.count()
            })

        except Subscription.DoesNotExist:

            return Response({'error': 'Subscription not found'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:

            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper(tags=['Superadmin Portal'], model='Audit Log')
    @action(detail=False, methods=['get'], url_path='audit-logs')
    def list_audit_logs(self, request):
        try:
            action_filter = request.query_params.get('action')
            start_date = request.query_params.get('start_date')
            end_date = request.query_params.get('end_date')

            queryset = AuditLog.objects.select_related('subscription__plan').all().order_by('-timestamp')

            if action_filter:
                queryset = queryset.filter(action=action_filter)
            if start_date and end_date:
                start_date = timezone.datetime.fromisoformat(start_date)
                end_date = timezone.datetime.fromisoformat(end_date)
                queryset = queryset.filter(timestamp__range=(start_date, end_date))

            audit_logs = queryset[:200]  # Limit to 200 most recent
            serializer = AuditLogSerializer(audit_logs, many=True)

            return Response({
                'count': audit_logs.count(),
                'total_filtered': queryset.count(),
                'results': serializer.data
            })

        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper(tags=['Superadmin Portal'], model='Webhook Event')
    @action(detail=True, methods=['post'], url_path='retry-webhook')
    def retry_webhook(self, request, pk=None):
        try:
            payment_service = PaymentService(request)
            result = payment_service.retry_webhook(webhook_event_id=pk)

            return Response(result)

        except Exception as e:

            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper(tags=['Superadmin Portal'], model='Webhook Event')
    @action(detail=False, methods=['get'], url_path='webhook-events')
    def list_webhook_events(self, request):
        try:
            status_filter = request.query_params.get('status')
            provider_filter = request.query_params.get('provider')
            start_date = request.query_params.get('start_date')
            end_date = request.query_params.get('end_date')

            queryset = WebhookEvent.objects.all().order_by('-created_at')

            if status_filter:
                queryset = queryset.filter(status=status_filter)
            if provider_filter:
                queryset = queryset.filter(provider=provider_filter)
            if start_date and end_date:
                start_date = timezone.datetime.fromisoformat(start_date)
                end_date = timezone.datetime.fromisoformat(end_date)
                queryset = queryset.filter(created_at__range=(start_date, end_date))

            webhook_events = queryset[:100]
            serializer = WebhookEventSerializer(webhook_events, many=True)

            return Response({
                'count': webhook_events.count(),
                'total_filtered': queryset.count(),
                'results': serializer.data
            })

        except Exception as e:
            logger.error(f"Webhook events listing failed: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper(tags=['Superadmin Portal'], model='Payment')
    @action(detail=False, methods=['get'], url_path='payments')
    def list_payments(self, request):
        """
        List/Search all payments in the system.
        Query params: status, provider, tenant_id, subscription_id, start_date, end_date
        Returns: payments + aggregation summary (total, completed, failed, revenue)
        """
        from apps.payment.models import Payment
        filters = {}
        for f in ['status','provider','subscription_id','plan_id']:
            v = request.query_params.get(f)
            if v: filters[f] = v
        if request.query_params.get('tenant_id'):
            filters['subscription__tenant_id'] = request.query_params['tenant_id']
        qs = Payment.objects.select_related('subscription','plan').filter(**filters)
        start, end = request.query_params.get('start_date'), request.query_params.get('end_date')
        if start and end:
            from django.utils.dateparse import parse_datetime
            qs = qs.filter(payment_date__range=(parse_datetime(start),parse_datetime(end)))
        summary = {
            'total': qs.count(),
            'completed': qs.filter(status='completed').count(),
            'failed': qs.filter(status='failed').count(),
            'total_revenue': qs.filter(status='completed').aggregate(total=Sum('amount'))['total'] or 0,
        }
        from apps.payment.serializers import PaymentSerializer
        results = PaymentSerializer(qs.order_by('-payment_date')[:100], many=True).data
        return Response({'results': results, 'summary': summary})

    @swagger_helper(tags=['Superadmin Portal'], model='RecurringToken')
    @action(detail=False, methods=['get'], url_path='recurring-tokens')
    def list_recurring_tokens(self, request):
        """
        List all RecurringTokens (active/inactive).
        Filtering: status, provider, tenant, subscription. Returns token summary info for dashboard.
        """
        from apps.billing.models import RecurringToken
        filters = {}
        for f in ['provider','is_active','subscription_id']:
            v = request.query_params.get(f)
            if v: filters[f] = v
        if request.query_params.get('tenant_id'):
            filters['subscription__tenant_id'] = request.query_params['tenant_id']
        qs = RecurringToken.objects.select_related('subscription').filter(**filters)
        from apps.billing.serializers import RecurringTokenSerializer
        total = qs.count()
        actives = qs.filter(is_active=True).count()
        results = RecurringTokenSerializer(qs.order_by('-created_at')[:100], many=True).data
        return Response({'total': total, 'active': actives, 'results': results})

    @swagger_helper(tags=['Superadmin Portal'], model='TrialUsage')
    @action(detail=False, methods=['get'], url_path='trials')
    def list_trials(self, request):
        """
        List/search all free trial usages and trial subscriptions (active, expired, tenant, machine).
        Query params: active, expired, tenant_id, machine, start_date, end_date
        Returns: trial list, conversion stats, and counts.
        """
        from apps.billing.models import TrialUsage, Subscription
        filters = {}
        if request.query_params.get('tenant_id'):
            filters['tenant_id'] = request.query_params['tenant_id']
        if request.query_params.get('machine'):
            filters['machine_number'] = request.query_params['machine']
        qs = TrialUsage.objects.filter(**filters)
        active = request.query_params.get('active')
        expired = request.query_params.get('expired')
        if active and not expired:
            qs = qs.filter(trial_end__gte=timezone.now())
        if expired and not active:
            qs = qs.filter(trial_end__lt=timezone.now())
        start, end = request.query_params.get('start_date'), request.query_params.get('end_date')
        if start and end:
            from django.utils.dateparse import parse_datetime
            qs = qs.filter(trial_start__range=(parse_datetime(start),parse_datetime(end)))
        # Conversion count = trial usages matched to a paid subscription by tenant
        converted_trial_count = Subscription.objects.filter(is_trial=False, status='active').values('tenant_id').distinct().count()
        from apps.billing.serializers import TrialUsageSerializer
        results = TrialUsageSerializer(qs.order_by('-trial_start')[:100], many=True).data
        summary = {
            'total_trials': qs.count(),
            'currently_active_trials': qs.filter(trial_end__gte=timezone.now()).count(),
            'converted_trials': converted_trial_count,
        }
        return Response({'results': results, 'summary': summary})

    @swagger_helper(tags=['Superadmin Portal'], model='Subscription')
    @action(detail=False, methods=['get'], url_path='expiring-soon')
    def list_expiring_soon(self, request):
        """
        List all subscriptions expiring in the next 7/14/30 days. Query param: days (default 14).
        Returns: subscriptions + summary count
        """
        days = int(request.query_params.get('days', '14'))
        from django.utils import timezone
        now = timezone.now()
        qs = Subscription.objects.filter(
            status='active',
            end_date__gt=now,
            end_date__lte=now+timedelta(days=days)
        )
        count = qs.count()
        serializer = SubscriptionSerializer(qs.order_by('end_date')[:100], many=True)
        return Response({'count': count, 'results': serializer.data})

    @swagger_helper(tags=['Superadmin Portal'], model='Subscription')
    @action(detail=False, methods=['get'], url_path='trials-ending-soon')
    def trials_ending_soon(self, request):
        """
        List all trial subscriptions ending in next 7 days. Returns: subscriptions and counts.
        """
        from django.utils import timezone
        now = timezone.now()
        soon = now + timedelta(days=7)
        qs = Subscription.objects.filter(status='trial', trial_end_date__gt=now, trial_end_date__lte=soon)
        count = qs.count()
        serializer = SubscriptionSerializer(qs.order_by('trial_end_date')[:100], many=True)
        return Response({'count': count, 'results': serializer.data})

    @swagger_helper(tags=['Superadmin Portal'], model='Subscription')
    @action(detail=False, methods=['get'], url_path='at-limit-or-high-usage')
    def expiring_high_usage(self, request):
        """
        List all active subscriptions with users/branches at >=90% of plan limits.
        Returns: subscription list and warning/alert count for dashboard.
        """
        qs = Subscription.objects.select_related('plan').filter(status='active')
        alerts = []
        for s in qs:
            # assuming s.current_user_count and s.current_branch_count exist
            if hasattr(s, 'current_user_count') and hasattr(s, 'current_branch_count'):
                if (
                    s.current_user_count >= 0.9*s.plan.max_users or
                    s.current_branch_count >= 0.9*s.plan.max_branches
                ):
                    alerts.append(s)
        serializer = SubscriptionSerializer(alerts, many=True)
        return Response({'count': len(alerts), 'results': serializer.data})

    @action(detail=False, methods=['get'], url_path='activity-feed')
    def activity_feed(self, request):
        """
        System-wide (global) activity feed for dashboard.
        Returns the 200 most recent events (subscription create/extend, payment, trial, plan change, failures, etc).
        """
        from apps.billing.models import Subscription, AuditLog
        from apps.payment.models import Payment
        from apps.billing.models import TrialUsage
        feed = []
        # AuditLog events (system actions)
        for log in AuditLog.objects.select_related('subscription').order_by('-timestamp')[:75]:
            feed.append({
                'type': f'audit:{log.action}',
                'timestamp': log.timestamp,
                'subscription_id': str(log.subscription_id) if log.subscription_id else None,
                'tenant_id': getattr(log.subscription, 'tenant_id', None) if hasattr(log, 'subscription') else None,
                'details': log.details,
            })
        # Payments (completed/failed)
        for p in Payment.objects.select_related('subscription').filter(status__in=['completed','failed']).order_by('-payment_date')[:75]:
            feed.append({
                'type': f'payment:{p.status}',
                'timestamp': p.payment_date,
                'subscription_id': str(p.subscription_id) if p.subscription_id else None,
                'tenant_id': getattr(p.subscription, 'tenant_id', None) if hasattr(p, 'subscription') else None,
                'amount': float(p.amount), 'provider': p.provider,
            })
        # Free trials started
        for t in TrialUsage.objects.order_by('-trial_start')[:30]:
            feed.append({
                'type': 'trial:start',
                'timestamp': t.trial_start,
                'tenant_id': t.tenant_id, 'machine_number': t.machine_number
            })
        # Subscription events: create, expired, canceled
        for s in Subscription.objects.order_by('-updated_at')[:25]:
            feed.append({
                'type': f'subscription:{s.status}',
                'timestamp': s.updated_at,
                'subscription_id': str(s.id),
                'tenant_id': s.tenant_id
            })
        # Sort all by descending timestamp
        feed.sort(key=lambda x: x['timestamp'], reverse=True)
        return Response({'count': len(feed), 'results': feed[:200]})