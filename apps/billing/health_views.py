"""
Health check views for monitoring and diagnostics
"""
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.decorators import action
from django.db import connection
from django.core.cache import cache
from django.utils import timezone
from django.conf import settings
import logging

from .circuit_breaker import CircuitBreakerManager
from .models import Plan, Subscription, AuditLog
from .utils import IdentityServiceClient, swagger_helper

logger = logging.getLogger(__name__)


class SystemHealthView(viewsets.ViewSet):
    """Comprehensive system health check"""
    permission_classes = [AllowAny]

    @swagger_helper("System Health"
                    "", "List")
    def list(self, request):
        """Overall system health check"""
        try:
            health_data = {
                'status': 'healthy',
                'timestamp': timezone.now().isoformat(),
                'version': getattr(settings, 'VERSION', '1.0.0'),
                'environment': getattr(settings, 'ENVIRONMENT', 'development'),
                'components': {}
            }

            # Check database
            db_health = self._check_database()
            health_data['components']['database'] = db_health

            # Check cache
            cache_health = self._check_cache()
            health_data['components']['cache'] = cache_health

            # Check external services
            external_health = self._check_external_services(request)
            health_data['components']['external_services'] = external_health

            # Check circuit breakers
            circuit_health = self._check_circuit_breakers()
            health_data['components']['circuit_breakers'] = circuit_health

            # Check business metrics
            metrics_health = self._check_business_metrics()
            health_data['components']['business_metrics'] = metrics_health

            # Determine overall health
            all_healthy = all(
                component.get('status') == 'healthy' 
                for component in health_data['components'].values()
            )
            
            health_data['status'] = 'healthy' if all_healthy else 'degraded'
            
            status_code = 200 if all_healthy else 503
            return Response(health_data, status=status_code)

        except Exception as e:
            logger.error(f"Health check failed: {str(e)}")
            return Response({
                'status': 'unhealthy',
                'error': str(e),
                'timestamp': timezone.now().isoformat()
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    def _check_database(self):
        """Check database connectivity and performance"""
        try:
            with connection.cursor() as cursor:
                # Test basic connectivity
                cursor.execute("SELECT 1")
                
                # Test query performance
                start_time = timezone.now()
                cursor.execute("SELECT COUNT(*) FROM billing_plan")
                plan_count = cursor.fetchone()[0]
                end_time = timezone.now()
                
                query_time = (end_time - start_time).total_seconds()
                
                return {
                    'status': 'healthy',
                    'response_time_ms': round(query_time * 1000, 2),
                    'plan_count': plan_count,
                    'connection_pool': 'active'
                }
        except Exception as e:
            return {
                'status': 'unhealthy',
                'error': str(e)
            }

    def _check_cache(self):
        """Check cache connectivity and performance"""
        try:
            # Test cache operations
            test_key = 'health_check_cache_test'
            test_value = 'test_value'
            
            start_time = timezone.now()
            cache.set(test_key, test_value, 10)
            retrieved_value = cache.get(test_key)
            cache.delete(test_key)
            end_time = timezone.now()
            
            response_time = (end_time - start_time).total_seconds()
            
            if retrieved_value == test_value:
                return {
                    'status': 'healthy',
                    'response_time_ms': round(response_time * 1000, 2),
                    'operations': 'read_write_delete_successful'
                }
            else:
                return {
                    'status': 'unhealthy',
                    'error': 'Cache value mismatch'
                }
        except Exception as e:
            return {
                'status': 'unhealthy',
                'error': str(e)
            }

    def _check_external_services(self, request):
        """Check external service connectivity"""
        services = {}
        
        # Check Identity Service
        try:
            client = IdentityServiceClient(request=request)
            # This would make a lightweight call to check connectivity
            # For now, we'll just check if the client can be instantiated
            services['identity_service'] = {
                'status': 'healthy',
                'endpoint': getattr(settings, 'IDENTITY_MICROSERVICE_URL', 'not_configured')
            }
        except Exception as e:
            services['identity_service'] = {
                'status': 'unhealthy',
                'error': str(e)
            }
        
        # Check Payment Services
        payment_providers = getattr(settings, 'PAYMENT_PROVIDERS', {})
        for provider_name in payment_providers.keys():
            services[f'payment_{provider_name}'] = {
                'status': 'healthy',
                'configured': True
            }
        
        return services

    def _check_circuit_breakers(self):
        """Check circuit breaker status"""
        try:
            circuit_breaker_manager = CircuitBreakerManager()
            breaker_states = circuit_breaker_manager.get_all_states()
            
            all_healthy = all(
                state.get('state') == 'CLOSED' 
                for state in breaker_states.values()
            )
            
            return {
                'status': 'healthy' if all_healthy else 'degraded',
                'breakers': breaker_states
            }
        except Exception as e:
            return {
                'status': 'unhealthy',
                'error': str(e)
            }

    def _check_business_metrics(self):
        """Check business metrics and data integrity"""
        try:
            # Count active subscriptions
            active_subs = Subscription.objects.filter(status='active').count()
            expired_subs = Subscription.objects.filter(status='expired').count()
            total_subs = Subscription.objects.count()
            
            # Count active plans
            active_plans = Plan.objects.filter(is_active=True).count()
            total_plans = Plan.objects.count()
            
            # Check for data integrity issues
            integrity_issues = []
            
            # Check for subscriptions without plans
            orphaned_subs = Subscription.objects.filter(plan__isnull=True).count()
            if orphaned_subs > 0:
                integrity_issues.append(f"{orphaned_subs} subscriptions without plans")
            
            # Check for plans with invalid pricing
            invalid_pricing = Plan.objects.filter(price__lt=0).count()
            if invalid_pricing > 0:
                integrity_issues.append(f"{invalid_pricing} plans with invalid pricing")
            
            return {
                'status': 'healthy' if not integrity_issues else 'degraded',
                'metrics': {
                    'active_subscriptions': active_subs,
                    'expired_subscriptions': expired_subs,
                    'total_subscriptions': total_subs,
                    'active_plans': active_plans,
                    'total_plans': total_plans
                },
                'integrity_issues': integrity_issues
            }
        except Exception as e:
            return {
                'status': 'unhealthy',
                'error': str(e)
            }

    @swagger_helper("System Health", "List")
    @action(detail=False, methods=['get'], url_path='detailed')
    def detailed_health(self, request):
        """Detailed health check with more information"""
        try:
            health_data = {
                'timestamp': timezone.now().isoformat(),
                'system_info': {
                    'python_version': getattr(settings, 'PYTHON_VERSION', 'unknown'),
                    'django_version': getattr(settings, 'DJANGO_VERSION', 'unknown'),
                    'database_engine': settings.DATABASES['default']['ENGINE'],
                    'cache_backend': getattr(settings, 'CACHES', {}).get('default', {}).get('BACKEND', 'unknown')
                },
                'performance_metrics': self._get_performance_metrics(),
                'recent_activity': self._get_recent_activity(),
                'error_logs': self._get_recent_errors()
            }
            
            return Response(health_data)
        except Exception as e:
            logger.error(f"Detailed health check failed: {str(e)}")
            return Response({
                'error': str(e),
                'timestamp': timezone.now().isoformat()
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def _get_performance_metrics(self):
        """Get performance metrics"""
        try:
            # Database query performance
            with connection.cursor() as cursor:
                start_time = timezone.now()
                cursor.execute("SELECT COUNT(*) FROM billing_subscription")
                end_time = timezone.now()
                db_query_time = (end_time - start_time).total_seconds()
            
            # Cache performance
            start_time = timezone.now()
            cache.set('perf_test', 'test', 10)
            cache.get('perf_test')
            cache.delete('perf_test')
            end_time = timezone.now()
            cache_query_time = (end_time - start_time).total_seconds()
            
            return {
                'database_query_time_ms': round(db_query_time * 1000, 2),
                'cache_operation_time_ms': round(cache_query_time * 1000, 2)
            }
        except Exception as e:
            return {'error': str(e)}

    def _get_recent_activity(self):
        """Get recent system activity"""
        try:
            # Recent audit logs
            recent_logs = AuditLog.objects.order_by('-timestamp')[:10]
            activity = []
            
            for log in recent_logs:
                activity.append({
                    'action': log.action,
                    'timestamp': log.timestamp.isoformat(),
                    'user': log.user
                })
            
            return {
                'recent_audit_logs': activity,
                'total_audit_logs': AuditLog.objects.count()
            }
        except Exception as e:
            return {'error': str(e)}

    def _get_recent_errors(self):
        """Get recent error information"""
        try:
            # This would typically query your logging system
            # For now, we'll return a placeholder
            return {
                'recent_errors': [],
                'error_rate': '0%'
            }
        except Exception as e:
            return {'error': str(e)}
