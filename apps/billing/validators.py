"""
Validation services for subscription management
"""
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.core.cache import cache
import uuid
import re
from typing import Dict, List, Any, Optional
import logging

from .models import Plan, Subscription
from .utils import IdentityServiceClient

logger = logging.getLogger(__name__)


class SubscriptionValidator:
    """Validator for subscription-related operations"""
    
    def __init__(self, request=None):
        self.request = request
    
    def validate_subscription_data(self, data: Dict[str, Any]) -> Dict[str, List[str]]:
        """Validate subscription input data"""
        errors = {}
        
        # Validate tenant_id format
        tenant_id = data.get('tenant_id')
        if not tenant_id:
            errors['tenant_id'] = ['Tenant ID is required']
        else:
            try:
                uuid.UUID(tenant_id)
            except (ValueError, TypeError):
                errors['tenant_id'] = ['Invalid tenant ID format']
        
        # Validate plan_id exists and is active
        plan_id = data.get('plan_id')
        if not plan_id:
            errors['plan_id'] = ['Plan ID is required']
        else:
            try:
                plan_uuid = uuid.UUID(plan_id)
                plan = Plan.objects.get(id=plan_uuid)
                if not plan.is_active or plan.discontinued:
                    errors['plan_id'] = ['Plan is not available']
            except (ValueError, TypeError):
                errors['plan_id'] = ['Invalid plan ID format']
            except Plan.DoesNotExist:
                errors['plan_id'] = ['Plan does not exist']
        
        # Validate dates
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        
        if start_date and end_date:
            try:
                if isinstance(start_date, str):
                    start_date = timezone.datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                if isinstance(end_date, str):
                    end_date = timezone.datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                
                if start_date >= end_date:
                    errors['dates'] = ['Start date must be before end date']
                
                if start_date < timezone.now():
                    errors['start_date'] = ['Start date cannot be in the past']
                    
            except (ValueError, TypeError):
                errors['dates'] = ['Invalid date format']
        
        # Validate business rules
        if not errors.get('tenant_id') and not errors.get('plan_id'):
            business_errors = self._validate_business_rules(tenant_id, plan_id)
            if business_errors:
                errors['business_rules'] = business_errors
        
        return errors
    
    def validate_plan_change_data(self, data: Dict[str, Any]) -> Dict[str, List[str]]:
        """Validate plan change data"""
        errors = {}
        
        # Validate subscription_id
        subscription_id = data.get('subscription_id')
        if not subscription_id:
            errors['subscription_id'] = ['Subscription ID is required']
        else:
            try:
                uuid.UUID(subscription_id)
                if not Subscription.objects.filter(id=subscription_id).exists():
                    errors['subscription_id'] = ['Subscription does not exist']
            except (ValueError, TypeError):
                errors['subscription_id'] = ['Invalid subscription ID format']
        
        # Validate new_plan_id
        new_plan_id = data.get('new_plan_id')
        if not new_plan_id:
            errors['new_plan_id'] = ['New plan ID is required']
        else:
            try:
                plan_uuid = uuid.UUID(new_plan_id)
                plan = Plan.objects.get(id=plan_uuid)
                if not plan.is_active or plan.discontinued:
                    errors['new_plan_id'] = ['New plan is not available']
            except (ValueError, TypeError):
                errors['new_plan_id'] = ['Invalid new plan ID format']
            except Plan.DoesNotExist:
                errors['new_plan_id'] = ['New plan does not exist']
        
        # Validate immediate flag
        immediate = data.get('immediate', False)
        if not isinstance(immediate, bool):
            errors['immediate'] = ['Immediate flag must be boolean']
        
        return errors
    
    def validate_payment_data(self, data: Dict[str, Any]) -> Dict[str, List[str]]:
        """Validate payment data"""
        errors = {}
        
        # Validate amount
        amount = data.get('amount')
        if amount is None:
            errors['amount'] = ['Amount is required']
        else:
            try:
                amount_decimal = float(amount)
                if amount_decimal <= 0:
                    errors['amount'] = ['Amount must be greater than 0']
            except (ValueError, TypeError):
                errors['amount'] = ['Invalid amount format']
        
        # Validate provider
        provider = data.get('provider')
        if not provider:
            errors['provider'] = ['Payment provider is required']
        elif provider not in ['flutterwave', 'paystack']:
            errors['provider'] = ['Invalid payment provider']
        
        # Validate currency
        currency = data.get('currency')
        if currency and currency != 'NGN':  # Assuming NGN is the only supported currency
            errors['currency'] = ['Unsupported currency']
        
        return errors
    
    def _validate_business_rules(self, tenant_id: str, plan_id: str) -> List[str]:
        """Validate business rules for subscription"""
        errors = []
        
        try:
            # Check if tenant already has active subscription
            tenant_uuid = uuid.UUID(tenant_id)
            existing_sub = Subscription.objects.filter(
                tenant_id=tenant_uuid,
                status__in=['active', 'trial', 'pending']
            ).first()
            
            if existing_sub:
                errors.append("Tenant already has an active subscription")
            
            # Check plan availability
            plan = Plan.objects.get(id=plan_id)
            
            # Check if plan is not discontinued
            if plan.discontinued:
                errors.append("Plan is no longer available")
            
            # Check if plan is active
            if not plan.is_active:
                errors.append("Plan is not currently active")
            
            # Check compliance requirements
            if plan.requires_compliance:
                # This would need to check tenant compliance status
                # For now, we'll assume compliance is checked elsewhere
                pass
            
            # Check regional availability
            if plan.regions:
                # This would need to check tenant region
                # For now, we'll assume region is checked elsewhere
                pass
            
        except (ValueError, TypeError, Plan.DoesNotExist) as e:
            errors.append(f"Validation error: {str(e)}")
        
        return errors


class UsageValidator:
    """Validator for usage-related operations"""
    
    def __init__(self, request=None):
        self.request = request
    
    def validate_usage_limits(self, tenant_id: str, plan: Plan) -> Dict[str, Any]:
        """Validate usage against plan limits"""
        try:
            if not self.request:
                return {
                    'valid': True,
                    'warnings': [],
                    'errors': [],
                    'usage': {}
                }
            
            client = IdentityServiceClient(request=self.request)
            users = client.get_users(tenant_id=tenant_id)
            branches = client.get_branches(tenant_id=tenant_id)
            
            current_users = len(users) if isinstance(users, list) else 0
            current_branches = len(branches) if isinstance(branches, list) else 0
            
            warnings = []
            errors = []
            
            # Check user limits
            if current_users > plan.max_users:
                errors.append(f"Current users ({current_users}) exceeds plan limit ({plan.max_users})")
            elif current_users > plan.max_users * 0.8:
                warnings.append(f"User usage is at {current_users/plan.max_users*100:.1f}% of plan limit")
            
            # Check branch limits
            if current_branches > plan.max_branches:
                errors.append(f"Current branches ({current_branches}) exceeds plan limit ({plan.max_branches})")
            elif current_branches > plan.max_branches * 0.8:
                warnings.append(f"Branch usage is at {current_branches/plan.max_branches*100:.1f}% of plan limit")
            
            return {
                'valid': len(errors) == 0,
                'warnings': warnings,
                'errors': errors,
                'usage': {
                    'users': {
                        'current': current_users,
                        'max': plan.max_users,
                        'percentage': (current_users / plan.max_users * 100) if plan.max_users > 0 else 0
                    },
                    'branches': {
                        'current': current_branches,
                        'max': plan.max_branches,
                        'percentage': (current_branches / plan.max_branches * 100) if plan.max_branches > 0 else 0
                    }
                }
            }
            
        except Exception as e:
            logger.error(f"Usage validation failed: {str(e)}")
            return {
                'valid': False,
                'warnings': [],
                'errors': [f"Usage validation failed: {str(e)}"],
                'usage': {}
            }
    
    def can_switch_plan(self, tenant_id: str, current_plan: Plan, new_plan: Plan) -> Dict[str, Any]:
        """Check if tenant can switch to new plan"""
        try:
            if not self.request:
                return {
                    'can_switch': True,
                    'warnings': [],
                    'errors': []
                }
            
            client = IdentityServiceClient(request=self.request)
            users = client.get_users(tenant_id=tenant_id)
            branches = client.get_branches(tenant_id=tenant_id)
            
            current_users = len(users) if isinstance(users, list) else 0
            current_branches = len(branches) if isinstance(branches, list) else 0
            
            warnings = []
            errors = []
            
            # Check if switching to a more restrictive plan
            if new_plan.max_users < current_plan.max_users:
                if current_users > new_plan.max_users:
                    errors.append(f"Cannot downgrade: current users ({current_users}) exceeds new plan limit ({new_plan.max_users})")
                elif current_users > new_plan.max_users * 0.8:
                    warnings.append(f"User usage will be at {current_users/new_plan.max_users*100:.1f}% of new plan limit")
            
            if new_plan.max_branches < current_plan.max_branches:
                if current_branches > new_plan.max_branches:
                    errors.append(f"Cannot downgrade: current branches ({current_branches}) exceeds new plan limit ({new_plan.max_branches})")
                elif current_branches > new_plan.max_branches * 0.8:
                    warnings.append(f"Branch usage will be at {current_branches/new_plan.max_branches*100:.1f}% of new plan limit")
            
            return {
                'can_switch': len(errors) == 0,
                'warnings': warnings,
                'errors': errors
            }
            
        except Exception as e:
            logger.error(f"Plan switch validation failed: {str(e)}")
            return {
                'can_switch': False,
                'warnings': [],
                'errors': [f"Plan switch validation failed: {str(e)}"]
            }


class InputValidator:
    """General input validation utilities"""
    
    @staticmethod
    def validate_uuid(value: str, field_name: str = "ID") -> Optional[str]:
        """Validate UUID format"""
        if not value:
            return f"{field_name} is required"
        
        try:
            uuid.UUID(value)
            return None
        except (ValueError, TypeError):
            return f"Invalid {field_name} format"
    
    @staticmethod
    def validate_email(email: str) -> Optional[str]:
        """Validate email format"""
        if not email:
            return "Email is required"
        
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(pattern, email):
            return "Invalid email format"
        
        return None
    
    @staticmethod
    def validate_phone(phone: str) -> Optional[str]:
        """Validate phone number format"""
        if not phone:
            return "Phone number is required"
        
        # Basic phone validation (adjust pattern as needed)
        pattern = r'^\+?[1-9]\d{1,14}$'
        if not re.match(pattern, phone):
            return "Invalid phone number format"
        
        return None
    
    @staticmethod
    def validate_positive_number(value: Any, field_name: str = "Number") -> Optional[str]:
        """Validate positive number"""
        if value is None:
            return f"{field_name} is required"
        
        try:
            num = float(value)
            if num <= 0:
                return f"{field_name} must be greater than 0"
            return None
        except (ValueError, TypeError):
            return f"Invalid {field_name} format"
    
    @staticmethod
    def validate_boolean(value: Any, field_name: str = "Boolean") -> Optional[str]:
        """Validate boolean value"""
        if value is None:
            return f"{field_name} is required"
        
        if not isinstance(value, bool):
            return f"{field_name} must be boolean"
        
        return None
    
    @staticmethod
    def validate_choice(value: str, choices: List[str], field_name: str = "Choice") -> Optional[str]:
        """Validate choice value"""
        if not value:
            return f"{field_name} is required"
        
        if value not in choices:
            return f"Invalid {field_name}. Must be one of: {', '.join(choices)}"
        
        return None


class RateLimitValidator:
    """Rate limiting validation"""
    
    def __init__(self):
        self.rate_limits = {
            'subscription_create': {'limit': 5, 'window': 3600},  # 5 per hour
            'subscription_update': {'limit': 10, 'window': 3600},  # 10 per hour
            'plan_change': {'limit': 3, 'window': 3600},  # 3 per hour
            'payment_initiate': {'limit': 10, 'window': 3600},  # 10 per hour
        }
    
    def check_rate_limit(self, action: str, identifier: str) -> Dict[str, Any]:
        """Check if action is within rate limit"""
        if action not in self.rate_limits:
            return {'allowed': True, 'remaining': None}
        
        limit_config = self.rate_limits[action]
        cache_key = f"rate_limit_{action}_{identifier}"
        
        # Get current count
        current_count = cache.get(cache_key, 0)
        
        if current_count >= limit_config['limit']:
            return {
                'allowed': False,
                'remaining': 0,
                'reset_time': cache.ttl(cache_key) or limit_config['window']
            }
        
        # Increment count
        cache.set(cache_key, current_count + 1, limit_config['window'])
        
        return {
            'allowed': True,
            'remaining': limit_config['limit'] - current_count - 1,
            'reset_time': limit_config['window']
        }
