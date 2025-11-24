from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Sum, Count, F, Q, Case, When, Value
from django.db.models.functions import Coalesce
from django.db.models.functions import TruncMonth, TruncDay
from django.db.models import Exists, OuterRef, DecimalField
from apps.billing.models import Subscription, AuditLog, Plan, TenantBillingPreferences
from apps.payment.models import Payment, WebhookEvent
from apps.payment.services import PaymentService
from apps.billing.serializers import SubscriptionSerializer, AuditLogSerializer, PlanSerializer
from .serializers import AnalyticsSerializer, WebhookEventSerializer
from datetime import datetime, timedelta
from .permissions import IsSuperuser
from .utils import swagger_helper
from .pagination import CustomPagination
import logging

logger = logging.getLogger(__name__)


class SuperadminPortalViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, IsSuperuser]

    @swagger_helper(tags=['Superadmin Portal'], model='Analytics')
    @action(detail=False, methods=['get'], url_path='analytics')
    def get_analytics(self, request):
        try:
            # Date range handling
            start_date = request.query_params.get('start_date')
            end_date = request.query_params.get('end_date')
            if start_date and end_date:
                start_date = timezone.datetime.fromisoformat(start_date)
                end_date = timezone.datetime.fromisoformat(end_date)
            else:
                end_date = timezone.now()
                start_date = end_date - timezone.timedelta(days=30)

            now = timezone.now()

            # ============================================================================
            # FINANCIAL METRICS
            # ============================================================================

            # 1. REVENUE METRICS
            # Total Revenue (all completed payments)
            total_revenue_all_time = Payment.objects.filter(
                status='completed'
            ).aggregate(total=Sum('amount'))['total'] or 0

            # Revenue in period
            period_revenue = Payment.objects.filter(
                payment_date__range=(start_date, end_date),
                status='completed'
            ).aggregate(total=Sum('amount'))['total'] or 0

            # Revenue by payment status
            revenue_by_status = Payment.objects.filter(
                payment_date__range=(start_date, end_date)
            ).values('status').annotate(
                total_amount=Sum('amount'),
                count=Count('id')
            ).order_by('status')

            # 2. MONTHLY RECURRING REVENUE (MRR) - Enhanced
            active_subscriptions = Subscription.objects.filter(
                status='active',
                end_date__gte=now
            ).select_related('plan')

            mrr = 0
            mrr_breakdown = {'monthly': 0, 'quarterly': 0, 'biannual': 0, 'annual': 0}
            for subscription in active_subscriptions:
                plan = subscription.plan
                if not plan:
                    continue
                price = float(plan.price)
                period = plan.billing_period

                if period == 'monthly':
                    mrr += price
                    mrr_breakdown['monthly'] += price
                elif period == 'quarterly':
                    monthly_equivalent = price / 3
                    mrr += monthly_equivalent
                    mrr_breakdown['quarterly'] += monthly_equivalent
                elif period == 'biannual':
                    monthly_equivalent = price / 6
                    mrr += monthly_equivalent
                    mrr_breakdown['biannual'] += monthly_equivalent
                elif period == 'annual':
                    monthly_equivalent = price / 12
                    mrr += monthly_equivalent
                    mrr_breakdown['annual'] += monthly_equivalent

            # 3. AVERAGE REVENUE PER USER (ARPU)
            active_customer_count = active_subscriptions.values('tenant_id').distinct().count()
            arpu = mrr / active_customer_count if active_customer_count else 0

            # 4. CUSTOMER LIFETIME VALUE (CLV) - Estimated
            avg_subscription_length_months = 12  # Assumption
            clv = arpu * avg_subscription_length_months

            # ============================================================================
            # SUBSCRIPTION METRICS
            # ============================================================================

            # Subscription status breakdown
            total_subscription_count = Subscription.objects.count()
            subscription_status_breakdown = Subscription.objects.values('status').annotate(
                count=Count('id'),
                percentage=Case(
                    When(count__gt=0, then=F('count') * 100.0 / total_subscription_count),
                    default=0,
                    output_field=DecimalField()
                ) if total_subscription_count > 0 else Value(0, output_field=DecimalField())
            ).order_by('-count')

            # Active subscriptions by plan
            active_by_plan = Subscription.objects.filter(
                status='active',
                end_date__gte=now
            ).values('plan__name', 'plan__tier_level').annotate(
                count=Count('id'),
                total_mrr=Sum(
                    Case(
                        When(plan__billing_period='monthly', then=F('plan__price')),
                        When(plan__billing_period='quarterly', then=F('plan__price')/3),
                        When(plan__billing_period='biannual', then=F('plan__price')/6),
                        When(plan__billing_period='annual', then=F('plan__price')/12),
                        default=0,
                        output_field=DecimalField()
                    )
                )
            ).order_by('-total_mrr')

            # 5. CHURN ANALYSIS - Enhanced
            # Churn rate calculation (canceled in period / active at start of period)
            initial_active = Subscription.objects.filter(
                status='active',
                start_date__lte=start_date
            ).count()

            canceled_in_period = Subscription.objects.filter(
                status='canceled',
                canceled_at__range=(start_date, end_date)
            ).count()

            churn_rate = (canceled_in_period / initial_active * 100) if initial_active else 0

            # Churn by plan
            churn_by_plan = Subscription.objects.filter(
                status='canceled',
                canceled_at__range=(start_date, end_date)
            ).values('plan__name').annotate(
                churn_count=Count('id')
            ).order_by('-churn_count')

            # 6. SUBSCRIPTION CHANGES (Upgrades/Downgrades)
            # Analyze audit logs for plan changes
            plan_changes = AuditLog.objects.filter(
                action='plan_changed',
                timestamp__range=(start_date, end_date)
            ).values('details').annotate(
                count=Count('id')
            )

            upgrades = 0
            downgrades = 0
            for change in plan_changes:
                details = change.get('details', {})
                if details.get('is_upgrade'):
                    upgrades += change['count']
                elif details.get('is_downgrade'):
                    downgrades += change['count']

            # ============================================================================
            # PAYMENT METRICS
            # ============================================================================

            # 7. PAYMENT SUCCESS RATES - Enhanced
            total_payments_period = Payment.objects.filter(
                payment_date__range=(start_date, end_date)
            ).count()

            completed_payments = Payment.objects.filter(
                payment_date__range=(start_date, end_date),
                status='completed'
            ).count()

            failed_payments = Payment.objects.filter(
                payment_date__range=(start_date, end_date),
                status='failed'
            ).count()

            pending_payments = Payment.objects.filter(
                payment_date__range=(start_date, end_date),
                status='pending'
            ).count()

            payment_success_rate = (completed_payments / total_payments_period * 100) if total_payments_period else 0
            payment_failure_rate = (failed_payments / total_payments_period * 100) if total_payments_period else 0

            # Payment methods performance
            payment_methods_performance = Payment.objects.filter(
                payment_date__range=(start_date, end_date)
            ).values('provider').annotate(
                total_count=Count('id'),
                success_count=Count('id', filter=Q(status='completed')),
                failed_count=Count('id', filter=Q(status='failed')),
                total_amount=Coalesce(Sum('amount', filter=Q(status='completed')), 0),
                success_rate=Case(
                    When(total_count__gt=0,
                         then=Count('id', filter=Q(status='completed')) * 100.0 / F('total_count')),
                    default=0,
                    output_field=DecimalField()
                )
            ).order_by('-total_count')

            # 8. OUTSTANDING RECEIVABLES
            # Pending payments that are overdue (payment_date > 7 days ago)
            overdue_threshold = now - timezone.timedelta(days=7)
            outstanding_receivables = Payment.objects.filter(
                status='pending',
                payment_date__lt=overdue_threshold
            ).aggregate(
                total_amount=Sum('amount'),
                count=Count('id')
            )

            # ============================================================================
            # USER GROWTH AND ENGAGEMENT
            # ============================================================================

            # 9. USER GROWTH TRENDS
            # New users by month (last 12 months)
            user_growth_trends = []
            for i in range(12):
                month_start = (now - timezone.timedelta(days=30*i)).replace(day=1, hour=0, minute=0, second=0)
                month_end = (month_start + timezone.timedelta(days=32)).replace(day=1) - timezone.timedelta(seconds=1)

                new_users = Subscription.objects.filter(
                    start_date__range=(month_start, month_end)
                ).values('tenant_id').distinct().count()

                user_growth_trends.append({
                    'month': month_start.strftime('%Y-%m'),
                    'new_users': new_users
                })

            # Total users (unique tenants)
            total_users = Subscription.objects.values('tenant_id').distinct().count()

            # Active users (tenants with active subscriptions)
            active_users = active_subscriptions.values('tenant_id').distinct().count()

            # 10. TRIAL METRICS - Enhanced
            currently_active_trials = Subscription.objects.filter(
                status='trial',
                trial_end_date__gte=now
            ).count()

            trials_started_period = Subscription.objects.filter(
                status='trial',
                start_date__range=(start_date, end_date)
            ).count()

            # Trial conversion (active subscriptions that started as trials within conversion window)
            conversion_window_days = 30
            trials_converted = Subscription.objects.filter(
                status='active',
                start_date__range=(start_date - timedelta(days=conversion_window_days), end_date)
            ).filter(
                Exists(TenantBillingPreferences.objects.filter(tenant_id=OuterRef('tenant_id')))
            ).count()

            trial_conversion_rate = (trials_converted / trials_started_period * 100) if trials_started_period else 0

            # Trial usage statistics
            from apps.billing.models import TrialUsage
            total_trial_signups = TrialUsage.objects.count()
            unique_trial_users = TrialUsage.objects.values('tenant_id').distinct().count()

            # ============================================================================
            # PLAN PERFORMANCE
            # ============================================================================

            # 11. PLAN PERFORMANCE METRICS
            plan_performance = []
            for plan in Plan.objects.filter(is_active=True, discontinued=False):
                total_subs = plan.subscription_set.count()
                active_subs = plan.subscription_set.filter(status='active', end_date__gte=now).count()
                trial_subs = plan.subscription_set.filter(status='trial').count()
                canceled_subs = plan.subscription_set.filter(status='canceled').count()
                total_revenue = plan.subscription_set.filter(
                    payments__status='completed'
                ).aggregate(total=Sum('payments__amount'))['total'] or 0

                churn_rate = (canceled_subs * 100.0 / total_subs) if total_subs > 0 else 0
                conversion_rate = (active_subs * 100.0 / total_subs) if total_subs > 0 else 0

                plan_performance.append({
                    'name': plan.name,
                    'price': float(plan.price),
                    'billing_period': plan.billing_period,
                    'tier_level': plan.tier_level,
                    'industry': plan.industry,
                    'total_subscriptions': total_subs,
                    'active_subscriptions': active_subs,
                    'trial_subscriptions': trial_subs,
                    'canceled_subscriptions': canceled_subs,
                    'total_revenue': float(total_revenue),
                    'churn_rate': float(churn_rate),
                    'conversion_rate': float(conversion_rate)
                })

            # Sort by active subscriptions descending
            plan_performance.sort(key=lambda x: x['active_subscriptions'], reverse=True)

            # ============================================================================
            # FINANCIAL PROJECTIONS
            # ============================================================================

            # 12. FINANCIAL PROJECTIONS
            # Projected MRR for next 3 months (assuming current growth rate)
            current_mrr = mrr

            # Calculate growth rate from last 3 months
            three_months_ago = now - timezone.timedelta(days=90)
            past_mrr_values = []

            for i in range(3):
                period_start = three_months_ago - timezone.timedelta(days=30*i)
                period_end = period_start + timezone.timedelta(days=30)

                period_mrr = 0
                period_active_subs = Subscription.objects.filter(
                    status='active',
                    start_date__lte=period_end,
                    end_date__gte=period_end
                ).select_related('plan')

                for sub in period_active_subs:
                    if sub.plan:
                        price = float(sub.plan.price)
                        period = sub.plan.billing_period
                        if period == 'monthly':
                            period_mrr += price
                        elif period == 'quarterly':
                            period_mrr += price / 3
                        elif period == 'biannual':
                            period_mrr += price / 6
                        elif period == 'annual':
                            period_mrr += price / 12

                past_mrr_values.append(period_mrr)

            # Simple linear growth projection
            if len(past_mrr_values) >= 2:
                growth_rate = (past_mrr_values[-1] - past_mrr_values[0]) / len(past_mrr_values)
            else:
                growth_rate = 0

            projected_mrr_3months = current_mrr + (growth_rate * 3)

            # ============================================================================
            # AUTO-RENEWAL METRICS
            # ============================================================================

            auto_renew_enabled = TenantBillingPreferences.objects.filter(
                auto_renew_enabled=True,
                renewal_status='active'
            ).count()

            auto_renew_disabled = TenantBillingPreferences.objects.filter(
                auto_renew_enabled=False
            ).count()

            renewal_failures = TenantBillingPreferences.objects.filter(
                renewal_failure_count__gt=0
            ).count()

            # ============================================================================
            # COMPREHENSIVE ANALYTICS RESPONSE
            # ============================================================================

            analytics_data = {
                'summary': {
                    'total_revenue': float(total_revenue_all_time),
                    'period_revenue': float(period_revenue),
                    'monthly_recurring_revenue': float(mrr),
                    'average_revenue_per_user': float(arpu),
                    'customer_lifetime_value': float(clv),
                    'total_users': total_users,
                    'active_users': active_users,
                    'churn_rate': float(churn_rate),
                    'payment_success_rate': float(payment_success_rate),
                    'trial_conversion_rate': float(trial_conversion_rate),
                    'auto_renewal_rate': (auto_renew_enabled / (auto_renew_enabled + auto_renew_disabled) * 100) if (auto_renew_enabled + auto_renew_disabled) else 0
                },

                'financial_metrics': {
                    'revenue': {
                        'total_all_time': float(total_revenue_all_time),
                        'in_period': float(period_revenue),
                        'by_status': list(revenue_by_status),
                        'average_transaction_value': float(period_revenue / completed_payments) if completed_payments else 0
                    },
                    'recurring_revenue': {
                        'mrr': float(mrr),
                        'breakdown_by_billing_period': mrr_breakdown,
                        'arpu': float(arpu),
                        'clv': float(clv),
                        'projected_mrr_3months': float(projected_mrr_3months)
                    }
                },

                'subscription_metrics': {
                    'status_breakdown': list(subscription_status_breakdown),
                    'active_by_plan': list(active_by_plan),
                    'churn_analysis': {
                        'rate': float(churn_rate),
                        'cancellations_in_period': canceled_in_period,
                        'by_plan': list(churn_by_plan)
                    },
                    'plan_changes': {
                        'upgrades': upgrades,
                        'downgrades': downgrades,
                        'net_change': upgrades - downgrades
                    },
                    'active_subscriptions': active_subscriptions.count(),
                    'trial_subscriptions': currently_active_trials
                },

                'payment_metrics': {
                    'success_rates': {
                        'overall_success_rate': float(payment_success_rate),
                        'failure_rate': float(payment_failure_rate),
                        'pending_rate': (pending_payments / total_payments_period * 100) if total_payments_period else 0
                    },
                    'by_provider': list(payment_methods_performance),
                    'outstanding_receivables': {
                        'amount': float(outstanding_receivables['total_amount'] or 0),
                        'count': outstanding_receivables['count'] or 0
                    },
                    'total_transactions': total_payments_period,
                    'completed_transactions': completed_payments,
                    'failed_transactions': failed_payments
                },

                'user_engagement': {
                    'growth_trends': user_growth_trends,
                    'trial_metrics': {
                        'currently_active': currently_active_trials,
                        'started_in_period': trials_started_period,
                        'converted_in_period': trials_converted,
                        'conversion_rate': float(trial_conversion_rate),
                        'total_trial_signups': total_trial_signups,
                        'unique_trial_users': unique_trial_users
                    },
                    'auto_renewal': {
                        'enabled': auto_renew_enabled,
                        'disabled': auto_renew_disabled,
                        'failure_count': renewal_failures
                    }
                },

                'plan_performance': list(plan_performance),

                'operational_efficiency': {
                    'audit_log_summary': list(AuditLog.objects.filter(
                        timestamp__range=(start_date, end_date)
                    ).values('action').annotate(count=Count('id')).order_by('-count')),
                    'system_health': {
                        'active_subscriptions_ratio': (active_subscriptions.count() / Subscription.objects.count() * 100) if Subscription.objects.count() else 0,
                        'payment_processing_efficiency': float(payment_success_rate),
                        'renewal_failure_rate': (renewal_failures / (auto_renew_enabled + auto_renew_disabled) * 100) if (auto_renew_enabled + auto_renew_disabled) else 0
                    }
                },

                'period': {
                    'start_date': start_date.isoformat(),
                    'end_date': end_date.isoformat(),
                    'days': (end_date - start_date).days
                },

                'generated_at': now.isoformat(),
                'data_freshness': 'real-time'
            }

            # Validate with serializer
            serializer = AnalyticsSerializer(data=analytics_data)
            serializer.is_valid(raise_exception=True)

            return Response(serializer.data)

        except Exception as e:
            logger.error(f"Enhanced analytics generation failed: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
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

            # Apply pagination
            paginator = CustomPagination()
            paginated_queryset = paginator.paginate_queryset(queryset, request)
            serializer = AuditLogSerializer(paginated_queryset, many=True)

            return paginator.get_paginated_response(serializer.data)

        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper(tags=['Superadmin Portal'], model='Audit Log')
    @action(detail=True, methods=['get'], url_path='audit-log-detail')
    def get_audit_log_detail(self, request, pk=None):
        try:
            audit_log = AuditLog.objects.select_related('subscription__plan').get(id=pk)
            serializer = AuditLogSerializer(audit_log)

            return Response(serializer.data)

        except AuditLog.DoesNotExist:
            return Response({'error': 'Audit log not found'}, status=status.HTTP_404_NOT_FOUND)
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

    @swagger_helper(tags=['Superadmin Portal'], model='TenantBillingPreferences')
    @action(detail=False, methods=['get'], url_path='billing-preferences')
    def list_billing_preferences(self, request):
        """
        List all TenantBillingPreferences (active/inactive auto-renewal).
        Filtering: auto_renew_enabled, renewal_status, tenant_id. Returns billing preferences for dashboard.
        """
        from apps.billing.models import TenantBillingPreferences
        filters = {}
        if request.query_params.get('auto_renew_enabled'):
            filters['auto_renew_enabled'] = request.query_params.get('auto_renew_enabled').lower() == 'true'
        if request.query_params.get('renewal_status'):
            filters['renewal_status'] = request.query_params.get('renewal_status')
        if request.query_params.get('tenant_id'):
            filters['tenant_id'] = request.query_params.get('tenant_id')

        qs = TenantBillingPreferences.objects.filter(**filters)
        total = qs.count()
        active_auto_renew = qs.filter(auto_renew_enabled=True, renewal_status='active').count()
        results = []
        for pref in qs.order_by('-updated_at')[:100]:
            results.append({
                'tenant_id': str(pref.tenant_id),
                'auto_renew_enabled': pref.auto_renew_enabled,
                'renewal_status': pref.renewal_status,
                'payment_provider': pref.payment_provider,
                'card_last4': pref.card_last4,
                'card_brand': pref.card_brand,
                'subscription_expiry_date': pref.subscription_expiry_date,
                'next_renewal_date': pref.next_renewal_date,
                'last_payment_at': pref.last_payment_at,
                'updated_at': pref.updated_at,
            })
        return Response({'total': total, 'active_auto_renew': active_auto_renew, 'results': results})

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