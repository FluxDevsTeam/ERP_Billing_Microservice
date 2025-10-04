from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Sum, Count, F
from django.db.models.functions import TruncMonth
from apps.billing.models import Subscription, AuditLog
from apps.payment.models import Payment, WebhookEvent
from apps.payment.services import PaymentService
from apps.billing.serializers import SubscriptionSerializer, AuditLogSerializer
from .serializers import AnalyticsSerializer, WebhookEventSerializer
import logging
from .permissions import IsSuperuser
from .utils import swagger_helper

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
                start_date__lte=end_date,
                end_date__gte=start_date
            ).select_related('plan')
            mrr = active_subscriptions.aggregate(
                total=Sum(F('plan__price') / F('plan__duration_days') * 30)
            )['total'] or 0     

            # Churn Rate
            canceled_subscriptions = Subscription.objects.filter(
                status='canceled',
                canceled_at__range=(start_date, end_date)
            ).count()
            total_subscriptions = Subscription.objects.filter(
                start_date__lte=end_date
            ).count()
            churn_rate = (canceled_subscriptions / total_subscriptions * 100) if total_subscriptions else 0

            # Trial Conversion Rate
            trials = Subscription.objects.filter(
                status='trial',
                trial_end_date__range=(start_date, end_date)
            ).count()
            converted_trials = Subscription.objects.filter(
                status='active',
                is_first_time_subscription=False,
                last_payment_date__range=(start_date, end_date)
            ).count()
            trial_conversion_rate = (converted_trials / trials * 100) if trials else 0

            # Revenue by Plan
            revenue_by_plan = Payment.objects.filter(
                payment_date__range=(start_date, end_date),
                status='completed'
            ).values('plan__name').annotate(
                total_amount=Sum('amount'),
                count=Count('id')
            )

            analytics_data = {
                'mrr': float(mrr),
                'churn_rate': float(churn_rate),
                'trial_conversion_rate': float(trial_conversion_rate),
                'revenue_by_plan': list(revenue_by_plan),
                'total_subscriptions': total_subscriptions,
                'active_subscriptions': active_subscriptions.count(),
                'timestamp': timezone.now().isoformat()
            }

            serializer = AnalyticsSerializer(data=analytics_data)
            serializer.is_valid(raise_exception=True)
            logger.info(f"Analytics retrieved for period {start_date} to {end_date}")
            return Response(serializer.data)

        except Exception as e:
            logger.error(f"Analytics retrieval failed: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper(tags=['Superadmin Portal'], model='Subscription')
    @action(detail=False, methods=['get'], url_path='subscriptions')
    def list_subscriptions(self, request):
        try:
            subscriptions = Subscription.objects.select_related('plan').all()
            serializer = SubscriptionSerializer(subscriptions, many=True)
            logger.info(f"Superadmin retrieved subscription list")
            return Response({
                'count': subscriptions.count(),
                'results': serializer.data
            })

        except Exception as e:
            logger.error(f"Subscription list retrieval failed: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper(tags=['Superadmin Portal'], model='Subscription Audit Log')
    @action(detail=True, methods=['get'], url_path='subscription-audit-logs')
    def get_subscription_audit_logs(self, request, pk=None):
        try:
            subscription = Subscription.objects.get(id=pk)
            audit_logs = subscription.audit_logs.all()[:100]
            serializer = AuditLogSerializer(audit_logs, many=True)
            logger.info(f"Audit logs retrieved for subscription {pk}")
            return Response({
                'subscription_id': str(subscription.id),
                'audit_logs': serializer.data,
                'count': audit_logs.count()
            })

        except Subscription.DoesNotExist:
            logger.error(f"Audit logs retrieval failed: Subscription {pk} not found")
            return Response({'error': 'Subscription not found'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Audit logs retrieval failed: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper(tags=['Superadmin Portal'], model='Webhook Event')
    @action(detail=True, methods=['post'], url_path='retry-webhook')
    def retry_webhook(self, request, pk=None):
        try:
            payment_service = PaymentService(request)
            result = payment_service.retry_webhook(webhook_event_id=pk)
            logger.info(f"Webhook retry initiated for event {pk}: {result['status']}")
            return Response(result)

        except Exception as e:
            logger.error(f"Webhook retry failed for event {pk}: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper(tags=['Superadmin Portal'], model='Webhook Event')
    @action(detail=False, methods=['get'], url_path='webhook-events')
    def list_webhook_events(self, request):
        try:
            webhook_events = WebhookEvent.objects.all().order_by('-created_at')[:100]
            serializer = WebhookEventSerializer(webhook_events, many=True)
            logger.info(f"Webhook events retrieved by superadmin")
            return Response({
                'count': webhook_events.count(),
                'results': serializer.data
            })

        except Exception as e:
            logger.error(f"Webhook events retrieval failed: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)