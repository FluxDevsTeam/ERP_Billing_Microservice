"""
Business logic services for subscription management
"""
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.conf import settings
import uuid
import logging
from typing import Optional, Dict, Any, Tuple
from decimal import Decimal

from .models import Plan, Subscription, AuditLog, SubscriptionCredit
from .utils import IdentityServiceClient
from .circuit_breaker import IdentityServiceCircuitBreaker
from .period_calculator import PeriodCalculator

logger = logging.getLogger(__name__)

TRIAL_DURATION_DAYS = int(getattr(settings, 'SUBSCRIPTION_TRIAL_DAYS', 7))  # Cast to int
# Handle None or invalid TRIAL_COOLDOWN_MONTHS
TRIAL_COOLDOWN_MONTHS_DEFAULT = 6
TRIAL_COOLDOWN_MONTHS_SETTING = getattr(settings, 'TRIAL_COOLDOWN_MONTHS', TRIAL_COOLDOWN_MONTHS_DEFAULT)
TRIAL_COOLDOWN_MONTHS = int(TRIAL_COOLDOWN_MONTHS_SETTING) if TRIAL_COOLDOWN_MONTHS_SETTING is not None else TRIAL_COOLDOWN_MONTHS_DEFAULT



class SubscriptionService:
    def __init__(self, request=None):
        self.request = request
        self.circuit_breaker = IdentityServiceCircuitBreaker()

    @transaction.atomic
    def create_subscription(self, tenant_id: str, plan_id: str, user: str = None) -> Tuple[Subscription, Dict[str, Any]]:
        try:
            tenant_uuid = uuid.UUID(tenant_id)
            plan_uuid = uuid.UUID(plan_id)
        except ValueError:
            logger.error(f"Subscription creation failed: Invalid tenant_id {tenant_id} or plan_id {plan_id}")
            raise ValidationError("Invalid tenant_id or plan_id format")

        plan = Plan.objects.get(id=plan_uuid)
        if not plan.is_active or plan.discontinued:
            logger.warning(f"Subscription creation failed: Plan {plan_id} is not available")
            raise ValidationError("Plan is not available")

        # Check for existing active subscription
        existing_sub = Subscription.objects.filter(
            tenant_id=tenant_uuid,
            status__in=['active', 'trial', 'pending']
        ).first()
        if existing_sub:
            logger.warning(f"Subscription creation blocked: Tenant {tenant_id} already has active subscription {existing_sub.id}")
            raise ValidationError("Active subscription already exists")

        # Trial eligibility check
        user_email = self.request.user.email if self.request and self.request.user else None
        is_trial = False
        trial_end_date = None
        if not Subscription.objects.filter(tenant_id=tenant_uuid).exists():
            # Check if tenant has had a trial in the last 6 months
            recent_trial = Subscription.objects.filter(
                tenant_id=tenant_uuid,
                status='trial',
                trial_end_date__gte=timezone.now() - timezone.timedelta(days=30 * TRIAL_COOLDOWN_MONTHS)
            ).exists()
            if recent_trial:
                logger.warning(f"Trial creation blocked: Tenant {tenant_id} had a recent trial")
                raise ValidationError("Trial period already used within cooldown period")
            is_trial = True
            trial_end_date = timezone.now() + timezone.timedelta(days=TRIAL_DURATION_DAYS)

        # Check tenant compliance and usage
        tenant_info = self._get_tenant_with_fallback(tenant_id)
        errors = self._validate_business_rules(tenant_info or {}, plan)
        if errors:
            logger.warning(f"Subscription creation failed for tenant {tenant_id}: {', '.join(errors)}")
            raise ValidationError(f"Cannot create subscription: {', '.join(errors)}")

        can_switch, switch_errors = self._check_usage_limits(tenant_id, plan)
        if not can_switch:
            logger.warning(f"Subscription creation failed for tenant {tenant_id}: {', '.join(switch_errors)}")
            raise ValidationError(f"Cannot create subscription: {', '.join(switch_errors)}")

        # Create subscription
        subscription = Subscription.objects.create(
            tenant_id=tenant_uuid,
            plan=plan,
            status='trial' if is_trial else 'active',
            start_date=timezone.now(),
            trial_end_date=trial_end_date,
            end_date=None,  # Will be set by calculate_end_date in save()
            next_payment_date=trial_end_date if is_trial else None,
            is_first_time_subscription=True,
            trial_used=is_trial
        )
        # Trigger dynamic end_date calculation
        subscription.save()

        self._audit_log(subscription, 'created', user, {
            'plan_name': plan.name,
            'tenant_id': str(tenant_id),
            'billing_period': plan.billing_period,
            'is_trial': is_trial,
            'user_email': user_email
        })

        logger.info(f"Subscription created for tenant {tenant_id} with plan {plan.name} - {'Trial' if is_trial else 'Active'}")
        return subscription, {
            'status': 'success',
            'subscription_id': str(subscription.id),
            'is_trial': is_trial
        }

    @transaction.atomic
    def renew_subscription(self, subscription_id: str, user: str = None) -> Tuple[Subscription, Dict[str, Any]]:
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            if not subscription.can_be_renewed():
                logger.warning(f"Subscription {subscription_id} renewal failed: Not eligible for renewal")
                raise ValidationError("Subscription cannot be renewed")

            base_date = subscription.end_date if subscription.end_date > timezone.now() else timezone.now()
            subscription.start_date = base_date
            subscription.end_date = None  # Trigger dynamic calculation
            subscription.status = 'active'
            subscription.last_payment_date = timezone.now()
            subscription.next_payment_date = None  # Will be set by calculate_end_date
            subscription.payment_retry_count = 0
            subscription.save()

            self._audit_log(subscription, 'renewed', user, {
                'new_end_date': subscription.end_date.isoformat(),
                'plan_name': subscription.plan.name,
                'billing_period': subscription.plan.billing_period
            })

            logger.info(f"Subscription {subscription_id} renewed until {subscription.end_date}")
            return subscription, {'status': 'success', 'new_end_date': subscription.end_date.isoformat()}

        except Subscription.DoesNotExist:
            logger.error(f"Subscription renewal failed: Subscription {subscription_id} not found")
            raise ValidationError("Subscription not found")
        except Exception as e:
            logger.error(f"Subscription renewal failed: {str(e)}")
            raise ValidationError(f"Subscription renewal failed: {str(e)}")

    @transaction.atomic
    def toggle_auto_renew(self, subscription_id: str, auto_renew: bool, user: str = None) -> Tuple[Subscription, Dict[str, Any]]:
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            if subscription.status not in ['active', 'trial']:
                logger.warning(f"Auto-renew toggle failed for subscription {subscription_id}: Invalid status {subscription.status}")
                raise ValidationError("Cannot toggle auto-renew for non-active subscription")

            subscription.auto_renew = auto_renew
            if not auto_renew:
                subscription.canceled_at = timezone.now() if subscription.status != 'trial' else None
                subscription.status = 'canceled' if subscription.status != 'trial' else 'trial'
            else:
                subscription.canceled_at = None
                subscription.status = 'active' if subscription.status != 'trial' else 'trial'
            subscription.save()

            self._audit_log(subscription, 'auto_renew_toggled', user, {
                'auto_renew': auto_renew,
                'previous_status': subscription.status,
                'tenant_id': str(subscription.tenant_id)
            })

            logger.info(f"Subscription {subscription_id} auto-renew set to {auto_renew}")
            return subscription, {'status': 'success', 'auto_renew': auto_renew}

        except Subscription.DoesNotExist:
            logger.error(f"Auto-renew toggle failed: Subscription {subscription_id} not found")
            raise ValidationError("Subscription not found")
        except Exception as e:
            logger.error(f"Auto-renew toggle failed: {str(e)}")
            raise ValidationError(f"Auto-renew toggle failed: {str(e)}")

    @transaction.atomic
    def suspend_subscription(self, subscription_id: str, user: str = None, reason: str = None) -> Tuple[Subscription, Dict[str, Any]]:
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            if subscription.status in ['suspended', 'canceled', 'expired']:
                logger.warning(f"Suspension failed for subscription {subscription_id}: Already {subscription.status}")
                raise ValidationError("Subscription cannot be suspended")

            subscription.status = 'suspended'
            subscription.suspended_at = timezone.now()
            subscription.save()

            self._audit_log(subscription, 'suspended', user, {
                'reason': reason,
                'suspended_at': subscription.suspended_at.isoformat()
            })

            logger.info(f"Subscription {subscription_id} suspended by {user}")
            return subscription, {'status': 'success', 'suspended_at': subscription.suspended_at.isoformat()}

        except Subscription.DoesNotExist:
            logger.error(f"Suspension failed: Subscription {subscription_id} not found")
            raise ValidationError("Subscription not found")
        except Exception as e:
            logger.error(f"Suspension failed: {str(e)}")
            raise ValidationError(f"Subscription suspension failed: {str(e)}")

    @transaction.atomic
    def change_plan(self, subscription_id: str, new_plan_id: str, user: str = None, immediate: bool = False) -> Tuple[Subscription, Dict[str, Any]]:
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            new_plan = Plan.objects.get(id=new_plan_id)

            if not new_plan.is_active or new_plan.discontinued:
                logger.warning(f"Plan change failed for subscription {subscription_id}: New plan {new_plan_id} is not available")
                raise ValidationError("New plan is not available")

            if subscription.plan.id == new_plan.id:
                logger.warning(f"Plan change failed for subscription {subscription_id}: Already on plan {new_plan.name}")
                raise ValidationError("Subscription is already on this plan")

            # No-downgrade policy
            now = timezone.now()
            if subscription.status == 'active' and subscription.end_date > now:
                if new_plan.tier_level < subscription.plan.tier_level and (now - subscription.start_date).days > 2:
                    logger.warning(f"Downgrade blocked for subscription {subscription_id}: New plan {new_plan.name} has lower tier")
                    raise ValidationError("Downgrades are not allowed during active subscription period")

            can_switch, switch_errors = self._can_switch_plan(subscription.tenant_id, new_plan)
            if not can_switch:
                logger.warning(f"Plan change failed for subscription {subscription_id}: {', '.join(switch_errors)}")
                raise ValidationError(f"Cannot switch plan: {', '.join(switch_errors)}")

            old_plan = subscription.plan
            prorated_amount = Decimal(0)
            remaining_days = 0

            if immediate:
                if subscription.status == 'active' and subscription.end_date > now:
                    remaining_days = (subscription.end_date - now).days
                    if remaining_days > 0:
                        # Calculate proration based on remaining time
                        old_period_days = PeriodCalculator.get_period_delta(old_plan.billing_period).days or 365
                        new_period_days = PeriodCalculator.get_period_delta(new_plan.billing_period).days or 365
                        unused_portion = (old_plan.price * remaining_days) / old_period_days
                        new_plan_cost = (new_plan.price * remaining_days) / new_period_days
                        prorated_amount = new_plan_cost - unused_portion
                        if prorated_amount < 0:
                            SubscriptionCredit.objects.create(
                                subscription=subscription,
                                amount=-prorated_amount,
                                reason='proration',
                                expires_at=now + timezone.timedelta(days=365)
                            )
                            self._audit_log(subscription, 'proration_credited', user, {
                                'amount': float(-prorated_amount),
                                'reason': 'Plan change proration',
                                'expires_at': (now + timezone.timedelta(days=365)).isoformat()
                            })

                subscription.plan = new_plan
                subscription.scheduled_plan = None
                subscription.status = 'active'
                subscription.start_date = now
                subscription.end_date = None  # Trigger dynamic calculation
                subscription.next_payment_date = None
            else:
                subscription.scheduled_plan = new_plan

            subscription.save()

            self._audit_log(subscription, 'plan_changed', user, {
                'old_plan_id': str(old_plan.id),
                'old_plan_name': old_plan.name,
                'new_plan_id': str(new_plan.id),
                'new_plan_name': new_plan.name,
                'immediate': immediate,
                'prorated_amount': float(prorated_amount),
                'remaining_days': remaining_days
            })

            logger.info(f"Subscription {subscription_id} plan changed from {old_plan.name} to {new_plan.name}")
            return subscription, {
                'status': 'success',
                'old_plan': old_plan.name,
                'new_plan': new_plan.name,
                'immediate': immediate,
                'prorated_amount': float(prorated_amount),
                'remaining_days': remaining_days,
                'requires_payment': prorated_amount > 0
            }

        except (Subscription.DoesNotExist, Plan.DoesNotExist):
            logger.error(f"Plan change failed: Subscription {subscription_id} or plan {new_plan_id} not found")
            raise ValidationError("Subscription or plan not found")
        except Exception as e:
            logger.error(f"Plan change failed: {str(e)}")
            raise ValidationError(f"Plan change failed: {str(e)}")

    @transaction.atomic
    def renew_in_advance(self, subscription_id: str, periods: int = 1, plan_id: str = None, user: str = None) -> Tuple[Subscription, Dict[str, Any]]:
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            plan = Plan.objects.get(id=plan_id) if plan_id else subscription.plan

            if not plan.is_active or plan.discontinued:
                logger.warning(f"Advance renewal failed for subscription {subscription_id}: Plan {plan_id} is not available")
                raise ValidationError("Plan is not available")

            if subscription.status not in ['active', 'expired']:
                logger.warning(f"Advance renewal failed for subscription {subscription_id}: Invalid status {subscription.status}")
                raise ValidationError("Cannot renew in advance for non-active or non-expired subscription")

            base_date = subscription.end_date if subscription.end_date > timezone.now() else timezone.now()
            subscription.plan = plan
            subscription.start_date = base_date
            subscription.end_date = None  # Trigger dynamic calculation
            subscription.status = 'active'
            subscription.last_payment_date = timezone.now()
            subscription.next_payment_date = None
            subscription.payment_retry_count = 0
            subscription.save()

            # Handle multiple periods by extending end_date
            for _ in range(periods - 1):
                subscription.start_date = subscription.end_date
                subscription.end_date = None
                subscription.save()

            self._audit_log(subscription, 'advance_renewed', user, {
                'periods': periods,
                'new_end_date': subscription.end_date.isoformat(),
                'plan_name': plan.name,
                'billing_period': plan.billing_period,
                'amount': float(plan.price * periods)
            })

            logger.info(f"Subscription {subscription_id} renewed in advance for {periods} periods")
            return subscription, {
                'status': 'success',
                'new_end_date': subscription.end_date.isoformat(),
                'periods': periods,
                'amount': float(plan.price * periods)
            }

        except (Subscription.DoesNotExist, Plan.DoesNotExist):
            logger.error(f"Advance renewal failed: Subscription {subscription_id} or plan {plan_id} not found")
            raise ValidationError("Subscription or plan not found")
        except Exception as e:
            logger.error(f"Advance renewal failed: {str(e)}")
            raise ValidationError(f"Advance renewal failed: {str(e)}")

    @transaction.atomic
    def check_expired_subscriptions(self) -> Dict[str, Any]:
        try:
            expired_subs = Subscription.objects.filter(
                end_date__lt=timezone.now(),
                status='active'
            )

            processed_count = 0
            for subscription in expired_subs:
                if subscription.is_in_grace_period():
                    logger.info(f"Subscription {subscription.id} is in grace period")
                    continue

                subscription.status = 'expired'
                if subscription.scheduled_plan:
                    subscription.plan = subscription.scheduled_plan
                    subscription.scheduled_plan = None
                    subscription.start_date = timezone.now()
                    subscription.end_date = None  # Trigger dynamic calculation
                    subscription.status = 'active'
                    subscription.save()
                    self._audit_log(subscription, 'plan_changed', 'system', {
                        'new_plan_id': str(subscription.plan.id),
                        'new_plan_name': subscription.plan.name,
                        'billing_period': subscription.plan.billing_period,
                        'reason': 'Scheduled plan applied after expiration'
                    })

                subscription.save()
                self._audit_log(subscription, 'expired', 'system', {
                    'expired_at': timezone.now().isoformat(),
                    'grace_period_days': settings.SUBSCRIPTION_GRACE_PERIOD_DAYS
                })

                processed_count += 1
                logger.info(f"Subscription {subscription.id} expired")

            return {
                'status': 'success',
                'processed_count': processed_count,
                'total_expired': expired_subs.count()
            }

        except Exception as e:
            logger.error(f"Expired subscription check failed: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _get_tenant_with_fallback(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        if not self.circuit_breaker.can_execute():
            return self._get_cached_tenant_data(tenant_id)

        try:
            if self.request:
                client = IdentityServiceClient(request=self.request)
                tenant = client.get_tenant(tenant_id=tenant_id)
                self.circuit_breaker.record_success()
                return tenant
            return None
        except Exception as e:
            self.circuit_breaker.record_failure()
            logger.warning(f"Identity service call failed, using fallback: {str(e)}")
            return self._get_cached_tenant_data(tenant_id)

    def _get_cached_tenant_data(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        cache_key = f"tenant_data_{tenant_id}"
        return cache.get(cache_key)

    def _validate_business_rules(self, tenant_info: Dict[str, Any], plan: Plan) -> list:
        errors = []
        tenant_industry = tenant_info.get('industry', 'Other')
        if plan.industry != 'Other' and plan.industry != tenant_industry:
            errors.append("Plan not available for tenant's industry")
        if plan.discontinued:
            errors.append("Plan is no longer available")
        return errors

    def _check_usage_limits(self, tenant_id: str, plan: Plan) -> Tuple[bool, list]:
        try:
            if self.request:
                client = IdentityServiceClient(request=self.request)
                users = client.get_users(tenant_id=tenant_id)
                branches = client.get_branches(tenant_id=tenant_id)

                current_users = len(users) if isinstance(users, list) else 0
                current_branches = len(branches) if isinstance(branches, list) else 0

                errors = []
                if current_users > plan.max_users:
                    errors.append(f"Current users ({current_users}) exceeds plan limit ({plan.max_users})")
                if current_branches > plan.max_branches:
                    errors.append(f"Current branches ({current_branches}) exceeds plan limit ({plan.max_branches})")
                return len(errors) == 0, errors
            return True, []
        except Exception as e:
            logger.warning(f"Usage limit check failed: {str(e)}")
            return True, []

    def _can_switch_plan(self, tenant_id: str, new_plan: Plan) -> Tuple[bool, list]:
        try:
            if self.request:
                client = IdentityServiceClient(request=self.request)
                users = client.get_users(tenant_id=tenant_id)
                branches = client.get_branches(tenant_id=tenant_id)

                current_users = len(users) if isinstance(users, list) else 0
                current_branches = len(branches) if isinstance(branches, list) else 0

                errors = []
                if current_users > new_plan.max_users:
                    errors.append(f"Cannot switch: current users ({current_users}) exceeds new plan limit ({new_plan.max_users})")
                if current_branches > new_plan.max_branches:
                    errors.append(f"Cannot switch: current branches ({current_branches}) exceeds new plan limit ({new_plan.max_branches})")
                return len(errors) == 0, errors
            return True, []
        except Exception as e:
            logger.warning(f"Plan switch check failed: {str(e)}")
            return True, []

    def _audit_log(self, subscription: Subscription, action: str, user: str = None, details: Dict[str, Any] = None):
        try:
            AuditLog.objects.create(
                subscription=subscription,
                action=action,
                user=user or 'system',
                details=details or {},
                ip_address=self._get_client_ip()
            )
        except Exception as e:
            logger.error(f"Audit logging failed for subscription {subscription.id}: {str(e)}")

    def _get_client_ip(self) -> Optional[str]:
        if self.request:
            x_forwarded_for = self.request.META.get('HTTP_X_FORWARDED_FOR')
            if x_forwarded_for:
                return x_forwarded_for.split(',')[0]
            return self.request.META.get('REMOTE_ADDR')
        return None


class UsageMonitorService:
    """Service for monitoring usage against plan limits"""

    def __init__(self, request=None):
        self.request = request

    def check_usage_limits(self, tenant_id: str) -> Dict[str, Any]:
        """Monitor usage against plan limits"""
        try:
            subscription = Subscription.objects.get(tenant_id=tenant_id)

            if subscription.status != 'active':
                return {
                    'status': 'inactive',
                    'message': 'Subscription is not active'
                }

            if self.request:
                client = IdentityServiceClient(request=self.request)
                users = client.get_users(tenant_id=tenant_id)
                branches = client.get_branches(tenant_id=tenant_id)

                current_users = len(users) if isinstance(users, list) else 0
                current_branches = len(branches) if isinstance(branches, list) else 0

                # Check soft limits (warnings)
                user_warning = current_users > subscription.plan.max_users * 0.8
                branch_warning = current_branches > subscription.plan.max_branches * 0.8

                # Check hard limits (blocking)
                user_blocked = current_users >= subscription.plan.max_users
                branch_blocked = current_branches >= subscription.plan.max_branches

                return {
                    'status': 'active',
                    'users': {
                        'current': current_users,
                        'max': subscription.plan.max_users,
                        'warning': user_warning,
                        'blocked': user_blocked,
                        'remaining': max(0, subscription.plan.max_users - current_users)
                    },
                    'branches': {
                        'current': current_branches,
                        'max': subscription.plan.max_branches,
                        'warning': branch_warning,
                        'blocked': branch_blocked,
                        'remaining': max(0, subscription.plan.max_branches - current_branches)
                    },
                    'overall_blocked': user_blocked or branch_blocked
                }
            else:
                return {
                    'status': 'unknown',
                    'message': 'Unable to check usage limits'
                }

        except Subscription.DoesNotExist:
            return {
                'status': 'not_found',
                'message': 'No subscription found for tenant'
            }
        except Exception as e:
            logger.error(f"Usage monitoring failed: {str(e)}")
            return {
                'status': 'error',
                'message': str(e)
            }

    def get_subscription_info(self, tenant_id: str) -> Dict[str, Any]:
        """Get subscription information without overage charges"""
        try:
            subscription = Subscription.objects.get(tenant_id=tenant_id)

            return {
                'subscription_id': str(subscription.id),
                'plan_name': subscription.plan.name,
                'status': subscription.status,
                'is_trial': subscription.status == 'trial',
                'trial_end_date': subscription.trial_end_date.isoformat() if subscription.trial_end_date else None,
                'end_date': subscription.end_date.isoformat() if subscription.end_date else None,
                'remaining_days': subscription.get_remaining_days(),
                'auto_renew': subscription.auto_renew
            }

        except Subscription.DoesNotExist:
            return {
                'error': 'No subscription found for tenant'
            }
        except Exception as e:
            logger.error(f"Subscription info retrieval failed: {str(e)}")
            return {
                'error': str(e)
            }


class PaymentRetryService:
    """Service for managing payment retries and dunning"""

    def __init__(self, request=None):
        self.request = request

    def should_retry_payment(self, subscription: Subscription) -> bool:
        """Check if payment should be retried"""
        if subscription.payment_retry_count >= subscription.max_payment_retries:
            return False

        # Check if enough time has passed since last retry
        if subscription.last_payment_date:
            retry_intervals = [1, 3, 7]  # days
            days_since_last = (timezone.now() - subscription.last_payment_date).days
            required_interval = retry_intervals[min(subscription.payment_retry_count, len(retry_intervals) - 1)]

            return days_since_last >= required_interval

        return True

    def increment_retry_count(self, subscription: Subscription) -> Subscription:
        """Increment payment retry count"""
        subscription.payment_retry_count += 1
        subscription.last_payment_date = timezone.now()
        subscription.save()
        return subscription

    def handle_failed_payment(self, subscription: Subscription) -> Dict[str, Any]:
        """Handle failed payment scenarios"""
        try:
            if subscription.status == 'active':
                # Check if we should retry
                if self.should_retry_payment(subscription):
                    # Increment retry count
                    subscription = self.increment_retry_count(subscription)

                    # Send payment reminder
                    self._send_payment_reminder(subscription)

                    return {
                        'status': 'retry_scheduled',
                        'retry_count': subscription.payment_retry_count,
                        'max_retries': subscription.max_payment_retries
                    }
                else:
                    # Max retries reached, suspend subscription
                    subscription.status = 'suspended'
                    subscription.suspended_at = timezone.now()
                    subscription.save()

                    # Disable tenant access
                    self._disable_tenant_access(subscription.tenant_id)

                    # Log the suspension
                    self._audit_log(subscription, 'suspended', 'system', {
                        'reason': 'max_payment_retries_reached',
                        'retry_count': subscription.payment_retry_count
                    })

                    return {
                        'status': 'suspended',
                        'reason': 'max_payment_retries_reached',
                        'retry_count': subscription.payment_retry_count
                    }
            else:
                return {
                    'status': 'no_action',
                    'reason': 'subscription_not_active'
                }

        except Exception as e:
            logger.error(f"Failed payment handling failed: {str(e)}")
            return {
                'status': 'error',
                'message': str(e)
            }

    def _send_payment_reminder(self, subscription: Subscription):
        """Send payment reminder to tenant"""
        logger.info(f"Payment reminder sent for subscription {subscription.id}")

    def _disable_tenant_access(self, tenant_id: str):
        """Disable tenant access"""
        logger.info(f"Tenant access disabled for {tenant_id}")

    def _audit_log(self, subscription: Subscription, action: str, user: str = None, details: Dict[str, Any] = None):
        """Log subscription changes for audit trail"""
        try:
            AuditLog.objects.create(
                subscription=subscription,
                action=action,
                user=user or 'system',
                details=details or {}
            )
        except Exception as e:
            logger.error(f"Audit logging failed: {str(e)}")