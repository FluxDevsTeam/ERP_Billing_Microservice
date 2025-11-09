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