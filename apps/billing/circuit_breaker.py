"""
Circuit breaker implementation for external service calls
"""
from django.utils import timezone
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Generic circuit breaker implementation"""
    
    def __init__(self, failure_threshold=5, timeout_seconds=60, expected_exception=Exception):
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
        self.expected_exception = expected_exception
        self.failure_count = 0
        self.last_failure_time = None
        self.state = 'CLOSED'  # CLOSED, OPEN, HALF_OPEN
    
    def can_execute(self):
        """Check if service calls are allowed"""
        if self.state == 'CLOSED':
            return True
        elif self.state == 'OPEN':
            if self._should_attempt_reset():
                self.state = 'HALF_OPEN'
                return True
            return False
        elif self.state == 'HALF_OPEN':
            return True
        return False
    
    def record_success(self):
        """Record successful call"""
        self.failure_count = 0
        self.state = 'CLOSED'
        logger.info("Circuit breaker: Service call successful, state reset to CLOSED")
    
    def record_failure(self):
        """Record failed call"""
        self.failure_count += 1
        self.last_failure_time = timezone.now()
        
        if self.failure_count >= self.failure_threshold:
            self.state = 'OPEN'
            logger.warning(f"Circuit breaker: Service call failed {self.failure_count} times, state changed to OPEN")
    
    def _should_attempt_reset(self):
        """Check if enough time has passed to attempt reset"""
        if self.last_failure_time is None:
            return True
        
        time_since_last_failure = timezone.now() - self.last_failure_time
        return time_since_last_failure >= timedelta(seconds=self.timeout_seconds)
    
    def get_state(self):
        """Get current circuit breaker state"""
        return {
            'state': self.state,
            'failure_count': self.failure_count,
            'last_failure_time': self.last_failure_time.isoformat() if self.last_failure_time else None,
            'can_execute': self.can_execute()
        }


class IdentityServiceCircuitBreaker(CircuitBreaker):
    """Circuit breaker specifically for Identity Service calls"""
    
    def __init__(self):
        super().__init__(
            failure_threshold=5,
            timeout_seconds=60,
            expected_exception=Exception
        )
        self.service_name = "Identity Service"
    
    def record_success(self):
        """Record successful Identity Service call"""
        super().record_success()
        logger.info(f"{self.service_name} circuit breaker: Service call successful")
    
    def record_failure(self):
        """Record failed Identity Service call"""
        super().record_failure()
        logger.error(f"{self.service_name} circuit breaker: Service call failed")


class PaymentServiceCircuitBreaker(CircuitBreaker):
    """Circuit breaker specifically for Payment Service calls"""
    
    def __init__(self):
        super().__init__(
            failure_threshold=3,
            timeout_seconds=120,
            expected_exception=Exception
        )
        self.service_name = "Payment Service"
    
    def record_success(self):
        """Record successful Payment Service call"""
        super().record_success()
        logger.info(f"{self.service_name} circuit breaker: Service call successful")
    
    def record_failure(self):
        """Record failed Payment Service call"""
        super().record_failure()
        logger.error(f"{self.service_name} circuit breaker: Service call failed")


class CircuitBreakerManager:
    """Manager for multiple circuit breakers"""
    
    def __init__(self):
        self.breakers = {
            'identity_service': IdentityServiceCircuitBreaker(),
            'payment_service': PaymentServiceCircuitBreaker()
        }
    
    def get_breaker(self, service_name: str) -> CircuitBreaker:
        """Get circuit breaker for specific service"""
        return self.breakers.get(service_name)
    
    def get_all_states(self):
        """Get states of all circuit breakers"""
        return {
            name: breaker.get_state() 
            for name, breaker in self.breakers.items()
        }
    
    def reset_breaker(self, service_name: str):
        """Reset specific circuit breaker"""
        if service_name in self.breakers:
            self.breakers[service_name].failure_count = 0
            self.breakers[service_name].last_failure_time = None
            self.breakers[service_name].state = 'CLOSED'
            logger.info(f"Circuit breaker for {service_name} has been reset")
    
    def reset_all_breakers(self):
        """Reset all circuit breakers"""
        for name in self.breakers:
            self.reset_breaker(name)
