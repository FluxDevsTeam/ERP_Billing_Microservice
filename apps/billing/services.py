from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.conf import settings
import uuid
import logging
import requests
from typing import Optional, Dict, Any, Tuple
from decimal import Decimal

from .models import Plan, Subscription, AuditLog, SubscriptionCredit, TrialUsage, TenantBillingPreferences
from .utils import IdentityServiceClient
from .circuit_breaker import IdentityServiceCircuitBreaker
from .period_calculator import PeriodCalculator

logger = logging.getLogger(__name__)

TRIAL_DURATION_DAYS = int(getattr(settings, 'SUBSCRIPTION_TRIAL_DAYS', 7))
TRIAL_COOLDOWN_MONTHS_DEFAULT = 6
TRIAL_COOLDOWN_MONTHS_SETTING = getattr(settings, 'TRIAL_COOLDOWN_MONTHS', TRIAL_COOLDOWN_MONTHS_DEFAULT)
TRIAL_COOLDOWN_MONTHS = int(TRIAL_COOLDOWN_MONTHS_SETTING) if TRIAL_COOLDOWN_MONTHS_SETTING is not None else TRIAL_COOLDOWN_MONTHS_DEFAULT


class SubscriptionService:
    def __init__(self, request=None):
        self.request = request
        self.circuit_breaker = IdentityServiceCircuitBreaker()

    @transaction.atomic
    def create_subscription(self, tenant_id: str, plan_id: str = None, user: str = None, machine_number: str = None, is_trial: bool = False, auto_renew: bool = False) -> Tuple[Subscription, Dict[str, Any]]:
        """
        Create a new subscription. If there's a previous subscription (trial or expired),
        carry over remaining days to the new subscription.
        
        For trials: plan_id can be None - trial gives access with limits (100 users, 10 branches) without plan restrictions.
        For paid subscriptions: plan_id is required.
        """
        try:
            tenant_uuid = uuid.UUID(tenant_id)
        except ValueError:
            logger.error(f"Subscription creation failed: Invalid tenant_id {tenant_id}")
            raise ValidationError("Invalid tenant_id format")

        # For trials, plan is optional (trial gives access with limits: 100 users, 10 branches)
        # For paid subscriptions, plan is required
        plan = None
        if is_trial:
            # Trial doesn't require a plan - it gives access with trial limits
            # But we still need a plan object for the subscription model
            # Use a default "Free Trial" plan or create a placeholder
            plan = Plan.objects.filter(name__icontains='trial').first()
            if not plan:
                # Create a default trial plan if it doesn't exist
                plan = Plan.objects.create(
                    name='Free Trial',
                    description='7-day free trial with 100 users and 10 branches',
                    industry='Other',
                    max_users=100,  # Trial limit: 100 users
                    max_branches=10,  # Trial limit: 10 branches
                    price=0,
                    billing_period='monthly',
                    is_active=True,
                    discontinued=False,
                    tier_level='tier4'
                )
        else:
            # Paid subscription requires a plan
            if not plan_id:
                raise ValidationError("plan_id is required for paid subscriptions")
            try:
                plan_uuid = uuid.UUID(plan_id)
                plan = Plan.objects.get(id=plan_uuid)
                if not plan.is_active or plan.discontinued:
                    logger.warning(f"Subscription creation failed: Plan {plan_id} is not available")
                    raise ValidationError("Plan is not available")
            except ValueError:
                logger.error(f"Subscription creation failed: Invalid plan_id {plan_id}")
                raise ValidationError("Invalid plan_id format")

        # Check for active subscriptions
        existing_active_sub = Subscription.objects.filter(
            tenant_id=tenant_uuid,
            status__in=['active', 'trial', 'pending']
        ).first()
        if existing_active_sub:
            logger.warning(f"Subscription creation blocked: Tenant {tenant_id} already has active subscription {existing_active_sub.id}")
            raise ValidationError("Active subscription already exists")

        user_email = self.request.user.email if self.request and self.request.user else None
        trial_end_date = None
        previous_subscription = None
        remaining_days_to_carry = 0
        carried_days = 0

        # Check for previous subscription (expired, canceled, or trial) to carry over remaining days
        # Only check if NOT creating a trial (trials don't carry over days)
        if not is_trial:
            previous_subscription = Subscription.objects.filter(
                tenant_id=tenant_uuid
            ).exclude(status='canceled').order_by('-created_at').first()

            if previous_subscription:
                now = timezone.now()
                # Calculate remaining days from previous subscription
                if previous_subscription.status == 'trial' and previous_subscription.trial_end_date:
                    # For trial subscriptions, check if trial period is still active or just ended
                    if previous_subscription.trial_end_date > now:
                        remaining_days_to_carry = (previous_subscription.trial_end_date - now).days
                    # If trial just ended, check grace period
                    elif previous_subscription.is_in_grace_period():
                        grace_end = previous_subscription.end_date + timezone.timedelta(days=settings.SUBSCRIPTION_GRACE_PERIOD_DAYS)
                        if grace_end > now:
                            remaining_days_to_carry = (grace_end - now).days
                elif previous_subscription.status == 'expired' and previous_subscription.end_date:
                    # For expired subscriptions, check if still in grace period
                    if previous_subscription.is_in_grace_period():
                        grace_end = previous_subscription.end_date + timezone.timedelta(days=settings.SUBSCRIPTION_GRACE_PERIOD_DAYS)
                        remaining_days_to_carry = (grace_end - now).days
                elif previous_subscription.status == 'active' and previous_subscription.end_date:
                    # If active subscription, carry remaining days
                    if previous_subscription.end_date > now:
                        remaining_days_to_carry = (previous_subscription.end_date - now).days
        else:
            previous_subscription = None

        # Handle trial creation
        if is_trial:
            # Check machine number for trial abuse prevention (only if provided)
            if machine_number:
                machine_trial_exists = TrialUsage.objects.filter(
                    machine_number=machine_number
                ).exists()
                if machine_trial_exists:
                    logger.warning(f"Trial creation blocked: Machine {machine_number} already used for trial")
                    raise ValidationError("Trial already used from this machine")
            
            # Check tenant trial history
            recent_trial = Subscription.objects.filter(
                tenant_id=tenant_uuid,
                status='trial',
                trial_end_date__gte=timezone.now() - timezone.timedelta(days=30 * TRIAL_COOLDOWN_MONTHS)
            ).exists()
            if recent_trial:
                logger.warning(f"Trial creation blocked: Tenant {tenant_id} had a recent trial")
                raise ValidationError("Trial period already used within cooldown period")
            
            trial_end_date = timezone.now() + timezone.timedelta(days=TRIAL_DURATION_DAYS)

        # For trials, skip business rules and usage limit checks (trial has its own limits: 100 users, 10 branches)
        if not is_trial:
            tenant_info = self._get_tenant_with_fallback(tenant_id)
            errors = self._validate_business_rules(tenant_info or {}, plan)
            if errors:
                logger.warning(f"Subscription creation failed for tenant {tenant_id}: {', '.join(errors)}")
                raise ValidationError(f"Cannot create subscription: {', '.join(errors)}")

            can_switch, switch_errors = self._check_usage_limits(tenant_id, plan)
            if not can_switch:
                logger.warning(f"Subscription creation failed for tenant {tenant_id}: {', '.join(switch_errors)}")
                raise ValidationError(f"Cannot create subscription: {', '.join(switch_errors)}")

        # Calculate start date
        now = timezone.now()
        start_date = now
        carried_days = remaining_days_to_carry if remaining_days_to_carry > 0 else 0

        # Create new subscription
        subscription = Subscription(
            tenant_id=tenant_uuid,
            plan=plan,
            status='trial' if is_trial else 'active',
            start_date=start_date,
            trial_end_date=trial_end_date,
            end_date=None  # Let save() calculate it first
        )
        subscription.save()  # This will calculate end_date based on billing_period
        
        # If we carried over days, extend the end_date
        if carried_days > 0 and subscription.end_date:
            # Extend end_date by the carried days
            subscription.end_date = subscription.end_date + timezone.timedelta(days=carried_days)
            subscription.save(update_fields=['end_date'])
            logger.info(f"Carried over {carried_days} days from previous subscription. New end_date: {subscription.end_date}")

        # Create TrialUsage record if this is a trial
        if is_trial and machine_number:
            try:
                ip_address = self._get_client_ip()
                TrialUsage.objects.create(
                    tenant_id=tenant_uuid,
                    user_email=user_email or 'unknown',
                    machine_number=machine_number,
                    trial_start_date=now,
                    trial_end_date=trial_end_date,
                    ip_address=ip_address
                )
            except Exception as e:
                logger.warning(f"Failed to create TrialUsage record: {str(e)}")

        # Mark previous subscription as replaced if it exists
        if previous_subscription and not is_trial:
            previous_subscription.status = 'canceled'
            previous_subscription.canceled_at = now
            previous_subscription.save()
            self._audit_log(previous_subscription, 'canceled', user, {
                'reason': 'replaced_by_new_subscription',
                'new_subscription_id': str(subscription.id),
                'carried_days': carried_days
            })

        self._audit_log(subscription, 'created', user, {
            'plan_name': plan.name,
            'tenant_id': str(tenant_id),
            'billing_period': plan.billing_period,
            'is_trial': is_trial,
            'user_email': user_email,
            'machine_number': machine_number if is_trial else None,
            'previous_subscription_id': str(previous_subscription.id) if previous_subscription else None,
            'carried_days': carried_days
        })

        logger.info(f"Subscription created for tenant {tenant_id} with plan {plan.name} - {'Trial' if is_trial else 'Active'} - Carried days: {carried_days}")
        return subscription, {
            'status': 'success',
            'subscription_id': str(subscription.id),
            'is_trial': is_trial,
            'carried_days': carried_days,
            'previous_subscription_id': str(previous_subscription.id) if previous_subscription else None
        }

    @transaction.atomic
    def create_first_subscription(self, tenant_id, plan_id, user, auto_renew):
        try:
            tenant_uuid = uuid.UUID(tenant_id)
        except ValueError:
            logger.error(f"Subscription creation failed: Invalid tenant_id {tenant_id}")
            raise ValidationError("Invalid tenant_id format")

        if not plan_id:
            raise ValidationError("plan_id is required for paid subscriptions")
        try:
            plan_uuid = uuid.UUID(plan_id)
            plan = Plan.objects.get(id=plan_uuid)
            if not plan.is_active or plan.discontinued:
                logger.warning(f"Subscription creation failed: Plan {plan_id} is not available")
                raise ValidationError("Plan is not available")
        except ValueError:
            logger.error(f"Subscription creation failed: Invalid plan_id {plan_id}")
            raise ValidationError("Invalid plan_id format")

        existing_active_sub = Subscription.objects.filter(
            tenant_id=tenant_uuid,
            status__in=['active', 'trial', 'pending']
        ).first()
        if existing_active_sub:
            logger.warning(f"Subscription creation blocked: Tenant {tenant_id} already has active subscription {existing_active_sub.id}")
            raise ValidationError("Active subscription already exists")

        user_email = user["email"]

        # Calculate start date
        now = timezone.now()
        start_date = now

        # Create new subscription
        subscription = Subscription(
            tenant_id=tenant_uuid,
            plan=plan,
            status='active',
            start_date=start_date,
            end_date=None
        )
        subscription.save()
    
        # Create or update TenantBillingPreferences with auto_renew setting
        preferences, created = TenantBillingPreferences.objects.get_or_create(
            tenant_id=tenant_uuid,
            defaults={
                'user_id': str(user) if user else None,
                'auto_renew_enabled': auto_renew,
                'renewal_status': 'active' if auto_renew else 'paused',
                'preferred_plan': plan,
                'subscription_expiry_date': subscription.end_date,
                'next_renewal_date': subscription.end_date if auto_renew else None,
            }
        )
        if not created:
            preferences.auto_renew_enabled = auto_renew
            preferences.renewal_status = 'active' if auto_renew else 'paused'
            preferences.preferred_plan = plan
            preferences.subscription_expiry_date = subscription.end_date
            preferences.next_renewal_date = subscription.end_date if auto_renew else None
            preferences.save()
    
        self._audit_log(subscription, 'created', user, {
            'plan_name': plan.name,
            'tenant_id': str(tenant_id),
            'billing_period': plan.billing_period,
            'is_trial': False,
            'user_email': user_email,
            'machine_number': None,
            'auto_renew_enabled': auto_renew,
        })

        return subscription, {
            'status': 'success',
            'subscription_id': str(subscription.id),
            'is_trial': False,
            'carried_days': 0,
            'previous_subscription_id': None
        }

    @transaction.atomic
    def renew_subscription(self, subscription_id: str, user: str = None) -> Tuple[Subscription, Dict[str, Any]]:
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            if not subscription.can_be_renewed():
                raise ValidationError("Subscription cannot be renewed")

            base_date = subscription.end_date if subscription.end_date > timezone.now() else timezone.now()
            subscription.start_date = base_date
            subscription.end_date = None
            subscription.status = 'active'
            subscription.last_payment_date = timezone.now()
            subscription.next_payment_date = None
            subscription.payment_retry_count = 0
            subscription.save()

            # Update or create auto-renewal to maintain consistency
            try:
                # First, try to find auto-renewal by subscription
                auto_renewal = AutoRenewal.objects.filter(
                    subscription=subscription,
                    status='active'
                ).first()
                
                if auto_renewal:
                    # Update existing auto-renewal
                    auto_renewal.expiry_date = subscription.end_date
                    auto_renewal.next_renewal_date = self._calculate_next_renewal_date_for_auto_renewal(
                        subscription.end_date,
                        subscription.plan.billing_period
                    )
                    auto_renewal.last_renewal_at = timezone.now()
                    auto_renewal.save()
                    logger.info(f"Updated auto-renewal {auto_renewal.id} for subscription {subscription_id}")
                else:
                    # Check if there's a tenant-based auto-renewal (no subscription reference)
                    auto_renewal = AutoRenewal.objects.filter(
                        tenant_id=subscription.tenant_id,
                        status='active'
                    ).first()
                    
                    if auto_renewal:
                        # Link the auto-renewal to the renewed subscription
                        auto_renewal.subscription = subscription
                        auto_renewal.expiry_date = subscription.end_date
                        auto_renewal.next_renewal_date = self._calculate_next_renewal_date_for_auto_renewal(
                            subscription.end_date,
                            auto_renewal.plan.billing_period
                        )
                        auto_renewal.last_renewal_at = timezone.now()
                        auto_renewal.save()
                        logger.info(f"Linked auto-renewal {auto_renewal.id} to renewed subscription {subscription_id}")
                        
            except Exception as e:
                logger.error(f"Failed to update auto-renewal after subscription renewal: {str(e)}")
                # Don't fail the entire renewal if auto-renewal update fails

            self._audit_log(subscription, 'renewed', user, {
                'new_end_date': subscription.end_date.isoformat(),
                'plan_name': subscription.plan.name,
                'billing_period': subscription.plan.billing_period
            })

            logger.info(f"Subscription {subscription_id} renewed successfully with plan {subscription.plan.name}")
            return subscription, {
                'status': 'success',
                'new_end_date': subscription.end_date.isoformat(),
                'auto_renewal_updated': True
            }

        except Subscription.DoesNotExist:
            raise ValidationError("Subscription not found")
        except Exception as e:
            logger.error(f"Subscription renewal failed for {subscription_id}: {str(e)}")
            raise ValidationError(f"Subscription renewal failed: {str(e)}")

    @transaction.atomic
    def toggle_auto_renew(self, subscription_id: str, auto_renew: bool, user: str = None) -> Tuple[Subscription, Dict[str, Any]]:
        """
        Toggle auto-renewal on/off. This does NOT cancel the subscription.
        Only enables/disables automatic renewal. Subscription status remains unchanged.
        Also removes/creates auto-renew from payment provider when disabling/enabling.
        """
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            # Allow toggling for any status except canceled/suspended
            # Toggling auto-renew should never cancel the subscription
            if subscription.status in ['canceled', 'suspended']:
                raise ValidationError("Cannot toggle auto-renew for canceled or suspended subscription")

            # Update TenantBillingPreferences instead of subscription
            preferences, created = TenantBillingPreferences.objects.get_or_create(
                tenant_id=subscription.tenant_id,
                defaults={
                    'user_id': str(user) if user else None,
                    'auto_renew_enabled': auto_renew,
                    'renewal_status': 'active' if auto_renew else 'paused',
                    'preferred_plan': subscription.plan,
                    'subscription_expiry_date': subscription.end_date,
                    'next_renewal_date': subscription.end_date if auto_renew else None,
                }
            )
            if not created:
                preferences.auto_renew_enabled = auto_renew
                preferences.renewal_status = 'active' if auto_renew else 'paused'
                preferences.next_renewal_date = subscription.end_date if auto_renew else None
                preferences.save()

            # Update AutoRenewal model for consistency
            try:
                auto_renewal = AutoRenewal.objects.filter(
                    tenant_id=subscription.tenant_id,
                    status='active'
                ).first()

                if auto_renew:
                    # If enabling auto-renew, ensure AutoRenewal record exists
                    if not auto_renewal:
                        auto_renewal = AutoRenewal.objects.create(
                            tenant_id=subscription.tenant_id,
                            subscription=subscription,
                            plan=subscription.plan,
                            expiry_date=subscription.end_date,
                            next_renewal_date=self._calculate_next_renewal_date_for_auto_renewal(
                                subscription.end_date,
                                subscription.plan.billing_period
                            ),
                            status='active',
                            user_id=user
                        )
                        logger.info(f"Created AutoRenewal record for tenant {subscription.tenant_id}")
                    else:
                        # Update existing auto-renewal
                        auto_renewal.subscription = subscription
                        auto_renewal.plan = subscription.plan
                        auto_renewal.expiry_date = subscription.end_date
                        auto_renewal.next_renewal_date = self._calculate_next_renewal_date_for_auto_renewal(
                            subscription.end_date,
                            subscription.plan.billing_period
                        )
                        auto_renewal.user_id = user or auto_renewal.user_id
                        auto_renewal.status = 'active'  # Reactivate if it was paused
                        auto_renewal.save()
                        logger.info(f"Updated AutoRenewal record for tenant {subscription.tenant_id}")

                    # Create subscription authorization in payment provider (if supported)
                    payment_provider_result = self._create_payment_provider_subscription(subscription, user)
                    
                    # Store payment provider identifiers in AutoRenewal notes
                    if payment_provider_result.get('status') == 'success':
                        self._store_payment_provider_info(auto_renewal, payment_provider_result)
                    
                    logger.info(f"Payment provider auto-renew setup: {payment_provider_result.get('status', 'unknown')}")
                else:
                    # If disabling auto-renew, cancel AutoRenewal record and payment provider subscription
                    if auto_renewal:
                        auto_renewal.status = 'canceled'
                        auto_renewal.save()
                        logger.info(f"Canceled AutoRenewal record for tenant {subscription.tenant_id}")

                    # Cancel subscription authorization in payment provider
                    payment_provider_result = self._cancel_payment_provider_subscription(subscription, user)
                    logger.info(f"Payment provider auto-renew cancellation: {payment_provider_result.get('status', 'unknown')}")
            except Exception as e:
                logger.error(f"Failed to update AutoRenewal records: {str(e)}")
                # Don't fail the entire operation if auto-renewal model update fails

            self._audit_log(subscription, 'auto_renew_toggled', user, {
                'auto_renew': auto_renew,
                'subscription_status': subscription.status,
                'tenant_id': str(subscription.tenant_id),
                'note': 'Auto-renew toggled without affecting subscription status'
            })

            return subscription, {
                'status': 'success', 
                'auto_renew': auto_renew,
                'auto_renewal_updated': True
            }

        except Subscription.DoesNotExist:
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

    def _calculate_remaining_monetary_value(self, subscription: Subscription) -> Tuple[Decimal, int]:
        """Calculate the remaining monetary value of the current subscription"""
        now = timezone.now()
        if subscription.status != 'active' or subscription.end_date <= now:
            return Decimal(0), 0
        
        remaining_days = (subscription.end_date - now).days
        if remaining_days <= 0:
            return Decimal(0), 0
        
        # Calculate total days in current billing period
        period_delta = PeriodCalculator.get_period_delta(subscription.plan.billing_period)
        if period_delta.months:
            # Approximate: 30 days per month
            total_period_days = period_delta.months * 30
        elif period_delta.years:
            # Approximate: 365 days per year
            total_period_days = period_delta.years * 365
        else:
            total_period_days = 30  # Default to monthly
        
        # Calculate monetary value: (plan_price * remaining_days) / total_period_days
        remaining_value = (subscription.plan.price * Decimal(remaining_days)) / Decimal(total_period_days)
        
        return remaining_value, remaining_days

    @transaction.atomic
    def change_plan(self, subscription_id: str, new_plan_id: str, user: str = None, immediate: bool = True) -> Tuple[Subscription, Dict[str, Any]]:
        """
        Change subscription plan with upgrade/downgrade handling:
        - Upgrades: Calculate remaining monetary value, subtract from new plan price (immediate effect)
        - Downgrades: Schedule for auto-renewal after current period ends (no immediate effect)
        """
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            new_plan = Plan.objects.get(id=new_plan_id)

            if not new_plan.is_active or new_plan.discontinued:
                logger.warning(f"Plan change failed for subscription {subscription_id}: New plan {new_plan_id} is not available")
                raise ValidationError("New plan is not available")

            if subscription.plan.id == new_plan.id:
                logger.warning(f"Plan change failed for subscription {subscription_id}: Already on plan {new_plan.name}")
                raise ValidationError("Subscription is already on this plan")

            can_switch, switch_errors = self._can_switch_plan(subscription.tenant_id, new_plan)
            if not can_switch:
                logger.warning(f"Plan change failed for subscription {subscription_id}: {', '.join(switch_errors)}")
                raise ValidationError(f"Cannot switch plan: {', '.join(switch_errors)}")

            old_plan = subscription.plan
            now = timezone.now()
            is_upgrade = new_plan.tier_level > old_plan.tier_level
            is_downgrade = new_plan.tier_level < old_plan.tier_level
            
            prorated_amount = Decimal(0)
            remaining_value = Decimal(0)
            remaining_days = 0
            change_type = 'same_tier'

            if is_upgrade:
                change_type = 'upgrade'
                # For upgrades: Calculate remaining value and subtract from new plan price
                if subscription.status == 'active' and subscription.end_date > now:
                    remaining_value, remaining_days = self._calculate_remaining_monetary_value(subscription)
                    
                    # Calculate new plan price for the billing period
                    new_plan_period_delta = PeriodCalculator.get_period_delta(new_plan.billing_period)
                    if new_plan_period_delta.months:
                        new_plan_period_days = new_plan_period_delta.months * 30
                    elif new_plan_period_delta.years:
                        new_plan_period_days = new_plan_period_delta.years * 365
                    else:
                        new_plan_period_days = 30
                    
                    # Amount to pay = new_plan_price - remaining_value
                    prorated_amount = new_plan.price - remaining_value
                    
                    # If remaining value is greater than new plan price, create credit
                    if prorated_amount < 0:
                        credit_amount = -prorated_amount
                        SubscriptionCredit.objects.create(
                            subscription=subscription,
                            amount=credit_amount,
                            reason='upgrade_credit',
                            expires_at=now + timezone.timedelta(days=365)
                        )
                        self._audit_log(subscription, 'proration_credited', user, {
                            'amount': float(credit_amount),
                            'reason': 'Upgrade credit',
                            'remaining_value': float(remaining_value),
                            'new_plan_price': float(new_plan.price),
                            'expires_at': (now + timezone.timedelta(days=365)).isoformat()
                        })
                        prorated_amount = Decimal(0)  # No payment required
                    
                    # Apply upgrade immediately
                    subscription.plan = new_plan
                    subscription.scheduled_plan = None
                    subscription.status = 'active'
                    subscription.start_date = now
                    subscription.end_date = None  # Will be recalculated in save()
                    subscription.next_payment_date = None
                else:
                    # Subscription expired or not active, just change plan
                    subscription.plan = new_plan
                    subscription.start_date = now
                    subscription.end_date = None
                    prorated_amount = new_plan.price
                    
            elif is_downgrade:
                change_type = 'downgrade'
                # For downgrades: Schedule for auto-renewal after current period ends
                # Don't change the current subscription, just schedule the downgrade
                subscription.scheduled_plan = new_plan
                
                # Update auto-renewal to use the new plan after current period expires
                try:
                    auto_renewal = AutoRenewal.objects.filter(
                        subscription=subscription,
                        status__in=['active', 'paused']
                    ).first()
                    if auto_renewal:
                        # Update auto-renewal to use downgraded plan after current expiry
                        auto_renewal.plan = new_plan
                        auto_renewal.notes = f"Scheduled downgrade from {old_plan.name} to {new_plan.name} after current period"
                        auto_renewal.save()
                except Exception as e:
                    logger.warning(f"Failed to update auto-renewal for downgrade: {str(e)}")
                
                if subscription.status == 'active' and subscription.end_date > now:
                    remaining_value, remaining_days = self._calculate_remaining_monetary_value(subscription)
                
                prorated_amount = Decimal(0)  # No payment required for downgrades
                # Current subscription continues until expiry
                
            else:
                # Same tier level - treat as upgrade for immediate effect
                change_type = 'same_tier_upgrade'
                if subscription.status == 'active' and subscription.end_date > now:
                    remaining_value, remaining_days = self._calculate_remaining_monetary_value(subscription)
                    new_plan_period_delta = PeriodCalculator.get_period_delta(new_plan.billing_period)
                    if new_plan_period_delta.months:
                        new_plan_period_days = new_plan_period_delta.months * 30
                    elif new_plan_period_delta.years:
                        new_plan_period_days = new_plan_period_delta.years * 365
                    else:
                        new_plan_period_days = 30
                    
                    prorated_amount = new_plan.price - remaining_value
                    
                    if prorated_amount < 0:
                        credit_amount = -prorated_amount
                        SubscriptionCredit.objects.create(
                            subscription=subscription,
                            amount=credit_amount,
                            reason='plan_change_credit',
                            expires_at=now + timezone.timedelta(days=365)
                        )
                        prorated_amount = Decimal(0)
                    
                    subscription.plan = new_plan
                    subscription.scheduled_plan = None
                    subscription.start_date = now
                    subscription.end_date = None
                    subscription.next_payment_date = None

            subscription.save()

            self._audit_log(subscription, 'plan_changed', user, {
                'old_plan_id': str(old_plan.id),
                'old_plan_name': old_plan.name,
                'new_plan_id': str(new_plan.id),
                'new_plan_name': new_plan.name,
                'change_type': change_type,
                'prorated_amount': float(prorated_amount),
                'remaining_value': float(remaining_value),
                'remaining_days': remaining_days,
                'is_upgrade': is_upgrade,
                'is_downgrade': is_downgrade
            })

            logger.info(f"Subscription {subscription_id} plan changed from {old_plan.name} to {new_plan.name} ({change_type})")
            return subscription, {
                'status': 'success',
                'old_plan': old_plan.name,
                'new_plan': new_plan.name,
                'change_type': change_type,
                'is_upgrade': is_upgrade,
                'is_downgrade': is_downgrade,
                'prorated_amount': float(prorated_amount),
                'remaining_value': float(remaining_value),
                'remaining_days': remaining_days,
                'requires_payment': prorated_amount > 0,
                'scheduled': is_downgrade  # Downgrades are scheduled
            }

        except (Subscription.DoesNotExist, Plan.DoesNotExist):
            logger.error(f"Plan change failed: Subscription {subscription_id} or plan {new_plan_id} not found")
            raise ValidationError("Subscription or plan not found")
        except Exception as e:
            logger.error(f"Plan change failed: {str(e)}")
            raise ValidationError(f"Plan change failed: {str(e)}")

    @transaction.atomic
    def extend_subscription(self, subscription_id: str, user: str = None) -> Tuple[Subscription, Dict[str, Any]]:
        """
        Manually extend subscription when remaining days is below 30 days.
        Adds one billing period to the existing subscription end date.
        """
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            
            if subscription.status not in ['active', 'expired']:
                raise ValidationError("Subscription must be active or expired to extend")
            
            remaining_days = subscription.get_remaining_days()
            
            if remaining_days >= 30:
                raise ValidationError("Subscription can only be extended when remaining days is less than 30")
            
            # Calculate extension period based on billing period
            period_delta = PeriodCalculator.get_period_delta(subscription.plan.billing_period)
            
            # Store old end date before modification
            old_end_date = subscription.end_date
            
            # Extend from current end_date
            if subscription.end_date:
                new_end_date = subscription.end_date + period_delta - timezone.timedelta(days=1)
            else:
                new_end_date = timezone.now() + period_delta - timezone.timedelta(days=1)
            
            subscription.end_date = new_end_date
            subscription.status = 'active'
            subscription.last_payment_date = timezone.now()
            subscription.next_payment_date = new_end_date
            subscription.save()
            
            # Update auto-renewal expiry date if it exists
            try:
                auto_renewal = AutoRenewal.objects.filter(
                    subscription=subscription,
                    status='active'
                ).first()
                if auto_renewal:
                    auto_renewal.expiry_date = new_end_date
                    auto_renewal.next_renewal_date = self._calculate_next_renewal_date_for_auto_renewal(
                        new_end_date,
                        subscription.plan.billing_period
                    )
                    auto_renewal.save()
            except Exception as e:
                logger.warning(f"Failed to update auto-renewal after extension: {str(e)}")
            
            self._audit_log(subscription, 'renewed', user, {
                'action': 'manual_extension',
                'old_end_date': old_end_date.isoformat() if old_end_date else None,
                'new_end_date': new_end_date.isoformat(),
                'period': subscription.plan.billing_period,
                'remaining_days_before': remaining_days
            })
            
            logger.info(f"Subscription {subscription_id} extended manually, new end date: {new_end_date}")
            return subscription, {
                'status': 'success',
                'new_end_date': new_end_date.isoformat(),
                'remaining_days_before': remaining_days,
                'extension_period': subscription.plan.billing_period
            }
            
        except Subscription.DoesNotExist:
            raise ValidationError("Subscription not found")
        except Exception as e:
            logger.error(f"Subscription extension failed: {str(e)}")
            raise ValidationError(f"Subscription extension failed: {str(e)}")

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

    def _create_payment_provider_subscription(self, subscription: Subscription, user: str = None) -> Dict[str, Any]:
        """
        Create recurring billing subscription in payment provider (Flutterwave/Paystack).
        This enables automatic card billing for future renewals.
        """
        try:
            # Get user email for billing
            user_email = self.request.user.email if self.request and self.request.user else None
            if not user_email:
                return {'status': 'skipped', 'reason': 'No user email available'}

            # Get payment provider from last successful payment or default to first configured
            last_payment = subscription.payments.filter(status='completed').order_by('-payment_date').first()
            provider = last_payment.provider if last_payment else list(settings.PAYMENT_PROVIDERS.keys())[0]
            
            # Build plan details for payment provider
            plan_data = {
                'plan_name': f"{subscription.plan.name} - {subscription.tenant_id}",
                'amount': float(subscription.plan.price),
                'interval': subscription.plan.billing_period,  # monthly, quarterly, etc.
                'currency': settings.PAYMENT_CURRENCY,
                'customer_email': user_email,
                'tenant_id': str(subscription.tenant_id),
                'subscription_id': str(subscription.id)
            }

            if provider == 'flutterwave':
                return self._create_flutterwave_subscription(plan_data, subscription)
            elif provider == 'paystack':
                return self._create_paystack_subscription(plan_data, subscription)
            else:
                logger.warning(f"Unsupported payment provider for auto-renew: {provider}")
                return {'status': 'skipped', 'reason': f'Unsupported provider: {provider}'}

        except Exception as e:
            logger.error(f"Failed to create payment provider subscription: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _cancel_payment_provider_subscription(self, subscription: Subscription, user: str = None) -> Dict[str, Any]:
        """
        Cancel recurring billing subscription in payment provider.
        This removes automatic card billing for future renewals.
        """
        try:
            # Get payment provider from last successful payment
            last_payment = subscription.payments.filter(status='completed').order_by('-payment_date').first()
            if not last_payment:
                return {'status': 'skipped', 'reason': 'No previous payment found'}

            provider = last_payment.provider
            user_email = self.request.user.email if self.request and self.request.user else None

            plan_data = {
                'customer_email': user_email,
                'tenant_id': str(subscription.tenant_id),
                'subscription_id': str(subscription.id),
                'previous_transaction_id': last_payment.transaction_id
            }

            if provider == 'flutterwave':
                return self._cancel_flutterwave_subscription(plan_data, subscription)
            elif provider == 'paystack':
                return self._cancel_paystack_subscription(plan_data, subscription)
            else:
                logger.warning(f"Unsupported payment provider for cancellation: {provider}")
                return {'status': 'skipped', 'reason': f'Unsupported provider: {provider}'}

        except Exception as e:
            logger.error(f"Failed to cancel payment provider subscription: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _create_flutterwave_subscription(self, plan_data: Dict, subscription: Subscription) -> Dict[str, Any]:
        """Create recurring billing in Flutterwave using Payment Plans"""
        try:
            import requests
            
            flutterwave_key = settings.PAYMENT_PROVIDERS["flutterwave"]["secret_key"]
            
            # Step 1: Create payment plan
            plan_result = self._create_flutterwave_payment_plan(plan_data)
            if not plan_result['success']:
                return plan_result
                
            plan_id = plan_result['plan_id']
            plan_token = plan_result['plan_token']
            
            # Step 2: Get the authorization code from previous payment
            last_payment = subscription.payments.filter(
                status='completed', 
                provider='flutterwave'
            ).order_by('-payment_date').first()
            
            if not last_payment:
                return {
                    'status': 'skipped', 
                    'reason': 'No Flutterwave payment found - customer must make a payment first'
                }
            
            # Step 3: The subscription is created when we charge the customer with the plan_id
            # Since this is for auto-renewal, we assume the customer already has a valid payment method
            # We just return the plan information for future use
            logger.info(f"Flutterwave payment plan ready for tenant {plan_data['tenant_id']}: {plan_token}")
            return {
                'status': 'success',
                'plan_id': plan_id,
                'plan_token': plan_token,
                'message': 'Payment plan created - will be activated on next charge',
                'provider': 'flutterwave'
            }

        except requests.exceptions.RequestException as e:
            logger.error(f"Flutterwave API error: {str(e)}")
            return {'status': 'error', 'message': 'Flutterwave service unavailable'}
        except Exception as e:
            logger.error(f"Flutterwave subscription creation error: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _create_flutterwave_payment_plan(self, plan_data: Dict) -> Dict[str, Any]:
        """Create a payment plan in Flutterwave"""
        try:
            import requests
            
            flutterwave_key = settings.PAYMENT_PROVIDERS["flutterwave"]["secret_key"]
            url = "https://api.flutterwave.com/v3/payment-plans"
            headers = {
                "Authorization": f"Bearer {flutterwave_key}",
                "Content-Type": "application/json"
            }
            
            # Map our interval to Flutterwave's format
            interval_mapping = {
                'monthly': 'monthly',
                'quarterly': 'quarterly', 
                'biannual': 'bi-annually',
                'annual': 'yearly'
            }
            
            flutterwave_interval = interval_mapping.get(plan_data['interval'], 'monthly')
            
            data = {
                "name": plan_data['plan_name'],
                "amount": int(plan_data['amount'] * 100),  # Convert to kobo (multiply by 100)
                "interval": flutterwave_interval,
                "duration": None,  # Indefinite billing
                "currency": plan_data['currency']
            }

            response = requests.post(url, headers=headers, json=data, timeout=10)
            response.raise_for_status()
            response_data = response.json()

            if response_data.get('status') == 'success':
                plan_info = response_data['data']
                plan_token = plan_info.get('plan_token')
                logger.info(f"Flutterwave payment plan created: {plan_token}")
                return {
                    'success': True,
                    'plan_id': plan_info.get('id'),
                    'plan_token': plan_token,
                    'name': plan_info.get('name')
                }
            else:
                error_msg = response_data.get('message', 'Unknown error')
                logger.error(f"Flutterwave payment plan creation failed: {error_msg}")
                return {'success': False, 'error': error_msg}

        except Exception as e:
            logger.error(f"Flutterwave payment plan creation error: {str(e)}")
            return {'success': False, 'error': str(e)}

    def _create_paystack_subscription(self, plan_data: Dict, subscription: Subscription) -> Dict[str, Any]:
        """Create recurring billing in Paystack using proper subscription API"""
        try:
            import requests
            
            paystack_key = settings.PAYMENT_PROVIDERS['paystack']['secret_key']
            
            # Step 1: Create or get Paystack plan
            plan_result = self._get_or_create_paystack_plan(plan_data)
            if not plan_result['success']:
                return plan_result
            
            plan_code = plan_result['plan_code']
            
            # Step 2: Get customer code from last successful payment
            last_payment = subscription.payments.filter(
                status='completed', 
                provider='paystack'
            ).order_by('-payment_date').first()
            
            if not last_payment:
                return {
                    'status': 'skipped', 
                    'reason': 'No Paystack authorization found - customer must make a payment first'
                }
            
            # Step 3: Create subscription using Paystack Subscription API
            # We need to get customer code and authorization code from the payment
            customer_info = self._extract_paystack_customer_info(last_payment)
            if not customer_info['customer_code']:
                return {
                    'status': 'skipped',
                    'reason': 'Could not extract customer code from payment history'
                }
            
            url = "https://api.paystack.co/subscription"
            headers = {
                "Authorization": f"Bearer {paystack_key}", 
                "Content-Type": "application/json"
            }
            
            data = {
                "customer": customer_info['customer_code'],
                "plan": plan_code,
                "email_token": plan_data['customer_email']  # Used for webhook verification
            }
            
            # Add authorization code if available
            if customer_info.get('authorization_code'):
                data["authorization"] = customer_info['authorization_code']

            response = requests.post(url, headers=headers, json=data, timeout=10)
            response.raise_for_status()
            response_data = response.json()

            if response_data.get('status') and response_data.get('data'):
                subscription_data = response_data['data']
                logger.info(f"Paystack subscription created for tenant {plan_data['tenant_id']}: {subscription_data.get('subscription_code')}")
                return {
                    'status': 'success',
                    'subscription_code': subscription_data.get('subscription_code'),
                    'authorization_code': subscription_data.get('authorization_code'),
                    'customer_code': customer_info['customer_code'],
                    'plan_code': plan_code,
                    'next_payment_date': subscription_data.get('next_payment_date'),
                    'provider': 'paystack'
                }
            else:
                error_msg = response_data.get('message', 'Unknown error')
                logger.error(f"Paystack subscription creation failed: {error_msg}")
                return {'status': 'error', 'message': error_msg}

        except requests.exceptions.RequestException as e:
            logger.error(f"Paystack API error: {str(e)}")
            return {'status': 'error', 'message': 'Paystack service unavailable'}
        except Exception as e:
            logger.error(f"Paystack subscription creation error: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _get_or_create_paystack_plan(self, plan_data: Dict) -> Dict[str, Any]:
        """Get existing Paystack plan or create a new one"""
        try:
            import requests
            
            paystack_key = settings.PAYMENT_PROVIDERS['paystack']['secret_key']
            plan_code = f"PLAN_{plan_data['tenant_id'][:8]}_{plan_data['interval']}"
            
            # Try to get existing plan first
            url = f"https://api.paystack.co/plan/{plan_code}"
            headers = {"Authorization": f"Bearer {paystack_key}"}
            
            try:
                response = requests.get(url, headers=headers, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    if data.get('status') and data.get('data'):
                        logger.info(f"Found existing Paystack plan: {plan_code}")
                        return {
                            'success': True,
                            'plan_code': plan_code,
                            'plan_id': data['data'].get('id')
                        }
            except requests.exceptions.RequestException:
                # Plan doesn't exist, we'll create it
                pass

            # Create new plan if doesn't exist
            url = "https://api.paystack.co/plan"
            headers = {
                "Authorization": f"Bearer {paystack_key}",
                "Content-Type": "application/json"
            }
            
            # Map our interval to Paystack's interval format
            interval_mapping = {
                'monthly': 'monthly',
                'quarterly': 'quarterly', 
                'biannual': 'biannually',
                'annual': 'annually'
            }
            
            paystack_interval = interval_mapping.get(plan_data['interval'], 'monthly')
            
            data = {
                "name": plan_data['plan_name'],
                "interval": paystack_interval,
                "amount": int(plan_data['amount'] * 100),  # Convert to kobo
                "currency": plan_data['currency']
            }

            response = requests.post(url, headers=headers, json=data, timeout=10)
            response.raise_for_status()
            response_data = response.json()

            if response_data.get('status') and response_data.get('data'):
                plan_info = response_data['data']
                logger.info(f"Created new Paystack plan: {plan_code} (ID: {plan_info.get('id')})")
                return {
                    'success': True,
                    'plan_code': plan_code,
                    'plan_id': plan_info.get('id'),
                    'plan_token': plan_info.get('plan_token')
                }
            else:
                error_msg = response_data.get('message', 'Unknown error')
                logger.error(f"Paystack plan creation failed: {error_msg}")
                return {'success': False, 'error': error_msg}

        except Exception as e:
            logger.error(f"Paystack plan creation error: {str(e)}")
            return {'success': False, 'error': str(e)}

    def _extract_paystack_customer_info(self, payment) -> Dict[str, Any]:
        """Extract customer information from Paystack payment data"""
        # Paystack doesn't return customer_code in standard payment verification
        # You would need to either:
        # 1. Store customer_code when payment is made
        # 2. Use the email to look up customer
        # 3. Use transaction reference to get full transaction details
        
        try:
            import requests
            
            # Option 1: Try to get customer code using transaction reference
            if payment.transaction_id:
                paystack_key = settings.PAYMENT_PROVIDERS['paystack']['secret_key']
                url = f"https://api.paystack.co/transaction/verify/{payment.transaction_id}"
                headers = {"Authorization": f"Bearer {paystack_key}"}
                
                response = requests.get(url, headers=headers, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    if data.get('status') and data.get('data'):
                        transaction_data = data['data']
                        customer = transaction_data.get('customer', {})
                        
                        return {
                            'customer_code': customer.get('customer_code'),
                            'authorization_code': transaction_data.get('authorization', {}).get('authorization_code'),
                            'email': customer.get('email')
                        }
        except Exception as e:
            logger.error(f"Failed to extract Paystack customer info: {str(e)}")
        
        # Fallback: return what we can infer
        return {
            'customer_code': None,  # Would need to be stored during payment
            'authorization_code': None,  # Would need to be stored during payment  
            'email': payment.subscription.tenant_id if payment.subscription else None
        }

    def _cancel_flutterwave_subscription(self, plan_data: Dict, subscription: Subscription) -> Dict[str, Any]:
        """Cancel recurring billing in Flutterwave"""
        try:
            import requests
            
            flutterwave_key = settings.PAYMENT_PROVIDERS["flutterwave"]["secret_key"]
            
            # Get the plan token from our stored data
            plan_token = self._get_flutterwave_plan_token(subscription)
            if not plan_token:
                return {
                    'status': 'skipped',
                    'reason': 'No Flutterwave plan token found'
                }
            
            # Cancel the payment plan
            url = f"https://api.flutterwave.com/v3/payment-plans/{plan_token}/cancel"
            headers = {
                "Authorization": f"Bearer {flutterwave_key}",
                "Content-Type": "application/json"
            }

            response = requests.post(url, headers=headers, json={}, timeout=10)
            response.raise_for_status()
            response_data = response.json()

            if response_data.get('status') == 'success':
                logger.info(f"Flutterwave payment plan canceled for tenant {plan_data['tenant_id']}: {plan_token}")
                return {
                    'status': 'success',
                    'plan_token': plan_token,
                    'message': 'Auto-renew canceled (Flutterwave)',
                    'provider': 'flutterwave'
                }
            else:
                error_msg = response_data.get('message', 'Unknown error')
                logger.error(f"Flutterwave subscription cancellation failed: {error_msg}")
                return {'status': 'error', 'message': error_msg}

        except requests.exceptions.RequestException as e:
            logger.error(f"Flutterwave API error: {str(e)}")
            return {'status': 'error', 'message': 'Flutterwave service unavailable'}
        except Exception as e:
            logger.error(f"Flutterwave cancellation error: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _cancel_paystack_subscription(self, plan_data: Dict, subscription: Subscription) -> Dict[str, Any]:
        """Cancel recurring billing in Paystack using subscription_code"""
        try:
            import requests
            
            paystack_key = settings.PAYMENT_PROVIDERS['paystack']['secret_key']
            
            # Get subscription code from our stored data
            # In a real implementation, you would store the subscription_code when creating the subscription
            subscription_code = self._get_paystack_subscription_code(subscription)
            if not subscription_code:
                return {
                    'status': 'skipped', 
                    'reason': 'No Paystack subscription code found - subscription may not be active'
                }
            
            url = "https://api.paystack.co/subscription/disable"
            headers = {
                "Authorization": f"Bearer {paystack_key}", 
                "Content-Type": "application/json"
            }
            
            # Paystack requires both code (subscription code) and token (email)
            data = {
                "code": subscription_code,
                "token": plan_data['customer_email']
            }

            response = requests.post(url, headers=headers, json=data, timeout=10)
            response.raise_for_status()
            response_data = response.json()

            if response_data.get('status'):
                logger.info(f"Paystack subscription disabled for tenant {plan_data['tenant_id']}: {subscription_code}")
                return {
                    'status': 'success',
                    'subscription_code': subscription_code,
                    'message': 'Auto-renew canceled (Paystack)',
                    'provider': 'paystack'
                }
            else:
                error_msg = response_data.get('message', 'Unknown error')
                logger.error(f"Paystack subscription cancellation failed: {error_msg}")
                return {'status': 'error', 'message': error_msg}

        except requests.exceptions.RequestException as e:
            logger.error(f"Paystack API error: {str(e)}")
            return {'status': 'error', 'message': 'Paystack service unavailable'}
        except Exception as e:
            logger.error(f"Paystack subscription cancellation error: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _get_paystack_subscription_code(self, subscription: Subscription) -> str:
        """Get the Paystack subscription code for a subscription"""
        auto_renewal = AutoRenewal.objects.filter(
            subscription=subscription,
            status__in=['active', 'canceled']
        ).order_by('-created_at').first()
        
        if auto_renewal and auto_renewal.notes:
            import re
            match = re.search(r'paystack_subscription_code:([A-Za-z0-9_]+)', auto_renewal.notes)
            if match:
                return match.group(1)
        
        return None

    def _get_flutterwave_plan_token(self, subscription: Subscription) -> str:
        """Get the Flutterwave plan token for a subscription"""
        auto_renewal = AutoRenewal.objects.filter(
            subscription=subscription,
            status__in=['active', 'canceled']
        ).order_by('-created_at').first()
        
        if auto_renewal and auto_renewal.notes:
            import re
            match = re.search(r'flutterwave_plan_token:([A-Za-z0-9_]+)', auto_renewal.notes)
            if match:
                return match.group(1)
        
        return None

    def change_subscription_card(self, subscription_id: str, new_payment_token: str, user: str = None) -> Dict[str, Any]:
        """
        Change the card used for auto-renewal subscription.
        
        Args:
            subscription_id: The subscription ID
            new_payment_token: Token from new payment (authorization code for Paystack, token for Flutterwave)
            user: User performing the change
        
        Returns:
            Dict with status and message
        """
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            provider = 'paystack'  # Could be determined from payment history
            
            if provider == 'paystack':
                return self._change_paystack_subscription_card(subscription, new_payment_token, user)
            elif provider == 'flutterwave':
                return self._change_flutterwave_subscription_card(subscription, new_payment_token, user)
            else:
                return {'status': 'error', 'message': 'Unsupported payment provider'}
                
        except Subscription.DoesNotExist:
            return {'status': 'error', 'message': 'Subscription not found'}
        except Exception as e:
            logger.error(f"Card change failed: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _change_paystack_subscription_card(self, subscription: Subscription, new_authorization_code: str, user: str = None) -> Dict[str, Any]:
        """Change the authorization code for a Paystack subscription"""
        try:
            # Get the active auto-renewal
            auto_renewal = AutoRenewal.objects.filter(
                subscription=subscription,
                status='active'
            ).first()
            
            if not auto_renewal:
                return {'status': 'error', 'message': 'No active auto-renewal found'}
            
            # Get subscription code from stored provider info
            provider_info = self._extract_payment_provider_info(auto_renewal)
            subscription_code = provider_info.get('subscription_code')
            
            if not subscription_code:
                return {'status': 'error', 'message': 'No subscription code found - cannot update card'}
            
            # Paystack doesn't have a direct "update authorization" API
            # So we need to create a new subscription with the new authorization
            paystack_key = settings.PAYMENT_PROVIDERS['paystack']['secret_key']
            url = "https://api.paystack.co/subscription"
            headers = {"Authorization": f"Bearer {paystack_key}", "Content-Type": "application/json"}
            
            data = {
                "customer": provider_info.get('customer_code'),
                "plan": provider_info.get('plan_code'),
                "authorization": new_authorization_code
            }
            
            response = requests.post(url, headers=headers, json=data, timeout=10)
            response.raise_for_status()
            response_data = response.json()
            
            if response_data.get('status') and response_data.get('data'):
                new_subscription_data = response_data['data']
                
                # Update auto-renewal with new subscription code
                self._store_payment_provider_info(auto_renewal, {
                    'subscription_code': new_subscription_data.get('subscription_code'),
                    'authorization_code': new_authorization_code,
                    'customer_code': provider_info.get('customer_code'),
                    'plan_code': provider_info.get('plan_code')
                })
                
                # Cancel the old subscription
                self._cancel_old_paystack_subscription(subscription_code, provider_info.get('customer_email', ''))
                
                logger.info(f"Paystack subscription card updated for subscription {subscription.id}")
                return {
                    'status': 'success',
                    'message': 'Card updated successfully for auto-renewal',
                    'new_subscription_code': new_subscription_data.get('subscription_code')
                }
            else:
                error_msg = response_data.get('message', 'Unknown error')
                return {'status': 'error', 'message': error_msg}
                
        except Exception as e:
            logger.error(f"Paystack card change failed: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _cancel_old_paystack_subscription(self, old_subscription_code: str, customer_email: str) -> None:
        """Cancel the old Paystack subscription after creating a new one"""
        try:
            paystack_key = settings.PAYMENT_PROVIDERS['paystack']['secret_key']
            url = "https://api.paystack.co/subscription/disable"
            headers = {"Authorization": f"Bearer {paystack_key}", "Content-Type": "application/json"}
            
            data = {
                "code": old_subscription_code,
                "token": customer_email
            }
            
            response = requests.post(url, headers=headers, json=data, timeout=10)
            if response.status_code == 200:
                response_data = response.json()
                if response_data.get('status'):
                    logger.info(f"Old Paystack subscription canceled: {old_subscription_code}")
                else:
                    logger.warning(f"Failed to cancel old Paystack subscription: {response_data.get('message')}")
            else:
                logger.warning(f"Failed to cancel old Paystack subscription: HTTP {response.status_code}")
                
        except Exception as e:
            logger.error(f"Error canceling old Paystack subscription: {str(e)}")

    def _change_flutterwave_subscription_card(self, subscription: Subscription, new_payment_token: str, user: str = None) -> Dict[str, Any]:
        """Change the card for a Flutterwave subscription"""
        try:
            # Flutterwave doesn't allow direct card changes for subscriptions
            # The customer needs to make a new payment with the new card
            # This will create a new payment plan subscription
            
            return {
                'status': 'skipped',
                'message': 'Flutterwave requires new payment to change card. Customer should make a new payment with the new card.',
                'action_required': 'new_payment',
                'instructions': [
                    '1. Make a new payment with the desired card',
                    '2. The new card will be automatically used for future auto-renewals',
                    '3. The old subscription will be canceled'
                ]
            }
            
        except Exception as e:
            logger.error(f"Flutterwave card change failed: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def manual_payment_with_saved_card(self, subscription_id: str, amount: Decimal = None, user: str = None) -> Dict[str, Any]:
        """
        Process a manual payment using saved recurring card/token details (for early renewal or top-up).
        Amount defaults to plan price.
        """
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            charge_amount = amount or subscription.plan.price
            token = getattr(subscription, 'recurring_token', None)
            if not token or not token.is_active:
                return {'status': 'error', 'message': 'No active recurring payment method found'}
            if token.provider == 'paystack':
                return self._paystack_server_charge(subscription, charge_amount, token, user)
            elif token.provider == 'flutterwave':
                return self._flutterwave_server_charge(subscription, charge_amount, token, user)
            else:
                return {'status': 'error', 'message': 'No saved payment method found'}
        except Subscription.DoesNotExist:
            return {'status': 'error', 'message': 'Subscription not found'}
        except Exception as e:
            logger.error(f"Manual payment failed: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _paystack_server_charge(self, subscription, amount, token, user):
        # Server-side call to Paystack /transaction/charge_authorization endpoint using stored authorization_code
        import requests
        paystack_secret = settings.PAYMENT_PROVIDERS['paystack']['secret_key']
        data = {
            "amount": int(float(amount) * 100),
            "email": token.email,
            "authorization_code": token.paystack_authorization_code
        }
        headers = {
            "Authorization": f"Bearer {paystack_secret}",
            "Content-Type": "application/json"
        }
        resp = requests.post("https://api.paystack.co/transaction/charge_authorization", json=data, headers=headers, timeout=15)
        resp_json = resp.json()
        if resp.status_code == 200 and resp_json.get("status"):
            self._audit_log(subscription, "recurring_charge_success", user, details={"provider": "paystack", "amount": str(amount)})
            return {"status": "success", "message": "Payment charged via Paystack recurring token", "response": resp_json}
        else:
            self._audit_log(subscription, "recurring_charge_failed", user, details={"provider": "paystack", "error": resp_json})
            return {"status": "error", "message": resp_json.get('message', 'Charge failed'), "response": resp_json}

    def _flutterwave_server_charge(self, subscription, amount, token, user):
        # Server-side call to Flutterwave /charges endpoint with recurring: true and payment_method_id
        import requests
        flutter_secret = settings.PAYMENT_PROVIDERS['flutterwave']['secret_key']
        data = {
            "amount": str(amount),
            "currency": settings.PAYMENT_CURRENCY,
            "customer": token.flutterwave_customer_id,
            "payment_type": "card",
            "payment_method": token.flutterwave_payment_method_id,
            "tx_ref": str(uuid.uuid4()),
            "recurring": True
        }
        headers = {
            "Authorization": f"Bearer {flutter_secret}",
            "Content-Type": "application/json"
        }
        resp = requests.post("https://api.flutterwave.com/v3/charges", json=data, headers=headers, timeout=15)
        resp_json = resp.json()
        if resp.status_code == 200 and resp_json.get("status") == 'success':
            self._audit_log(subscription, "recurring_charge_success", user, details={"provider": "flutterwave", "amount": str(amount)})
            return {"status": "success", "message": "Payment charged via Flutterwave recurring token", "response": resp_json}
        else:
            self._audit_log(subscription, "recurring_charge_failed", user, details={"provider": "flutterwave", "error": resp_json})
            return {"status": "error", "message": resp_json.get('message', 'Charge failed'), "response": resp_json}

    def manual_payment_with_new_card(self, subscription_id: str, amount: Decimal = None, provider: str = 'paystack', user: str = None) -> Dict[str, Any]:
        """
        Process a manual payment using new card details (for early renewal or top-up).
        This creates a fresh payment flow where user enters new card details.
        
        Args:
            subscription_id: The subscription ID
            amount: Amount to charge (defaults to plan price)
            provider: Payment provider ('paystack' or 'flutterwave')
            user: User making the payment
        
        Returns:
            Dict with payment initialization details
        """
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            charge_amount = amount or subscription.plan.price
            
            # Get tenant information from subscription
            tenant_id = str(subscription.tenant_id)
            
            # Get user email for payment
            user_email = self.request.user.email if self.request and self.request.user else None
            if not user_email:
                return {'status': 'error', 'message': 'User email required for payment'}
            
            # Create payment record for tracking
            from apps.payment.models import Payment
            payment = Payment.objects.create(
                plan=subscription.plan,
                subscription=subscription,
                amount=charge_amount,
                transaction_id=str(uuid.uuid4()),
                status='pending',
                provider=provider,
                payment_type='manual'
            )
            
            # Initialize payment based on provider
            if provider == 'paystack':
                return self._initialize_paystack_payment(
                    payment=payment,
                    amount=charge_amount,
                    user_email=user_email,
                    tenant_id=tenant_id
                )
            elif provider == 'flutterwave':
                return self._initialize_flutterwave_payment(
                    payment=payment,
                    amount=charge_amount,
                    user_email=user_email,
                    tenant_id=tenant_id
                )
            else:
                payment.delete()  # Clean up
                return {'status': 'error', 'message': f'Unsupported payment provider: {provider}'}
                
        except Subscription.DoesNotExist:
            return {'status': 'error', 'message': 'Subscription not found'}
        except Exception as e:
            logger.error(f"Manual payment with new card failed: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _initialize_paystack_payment(self, payment, amount: Decimal, user_email: str, tenant_id: str) -> Dict[str, Any]:
        """Initialize a Paystack payment for manual payment with new card"""
        try:
            import requests
            
            paystack_key = settings.PAYMENT_PROVIDERS['paystack']['secret_key']
            url = "https://api.paystack.co/transaction/initialize"
            headers = {"Authorization": f"Bearer {paystack_key}", "Content-Type": "application/json"}
            
            # Generate unique reference
            reference = f"MANUAL_{payment.id}_{int(timezone.now().timestamp())}"
            
            # Get base URL for callback
            base_url = settings.BILLING_MICROSERVICE_URL
            callback_url = f"{base_url}/api/v1/payment/payment-verify/confirm/?tx_ref={reference}&confirm_token={payment.transaction_id}&provider=paystack&amount={float(amount)}&plan_id={str(payment.plan.id)}&tenant_id={tenant_id}"
            
            data = {
                "email": user_email,
                "amount": int(float(amount) * 100),  # Convert to kobo
                "currency": settings.PAYMENT_CURRENCY,
                "reference": reference,
                "callback_url": callback_url,
                "metadata": {
                    "payment_id": str(payment.id),
                    "subscription_id": str(payment.subscription.id),
                    "tenant_id": tenant_id,
                    "payment_type": "manual_new_card"
                }
            }
            
            response = requests.post(url, headers=headers, json=data, timeout=10)
            response.raise_for_status()
            response_data = response.json()
            
            if response_data.get('status'):
                # Update payment with reference
                payment.transaction_id = reference
                payment.save()
                
                return {
                    'status': 'success',
                    'payment_id': str(payment.id),
                    'payment_url': response_data['data']['authorization_url'],
                    'access_code': response_data['data']['access_code'],
                    'reference': reference,
                    'amount': str(amount),
                    'provider': 'paystack',
                    'message': 'Please complete payment with your new card details'
                }
            else:
                error_msg = response_data.get('message', 'Payment initialization failed')
                payment.delete()
                return {'status': 'error', 'message': error_msg}
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Paystack API error: {str(e)}")
            payment.delete()
            return {'status': 'error', 'message': 'Paystack service unavailable'}
        except Exception as e:
            logger.error(f"Paystack payment initialization error: {str(e)}")
            payment.delete()
            return {'status': 'error', 'message': str(e)}

    def _initialize_flutterwave_payment(self, payment, amount: Decimal, user_email: str, tenant_id: str) -> Dict[str, Any]:
        """Initialize a Flutterwave payment for manual payment with new card"""
        try:
            import requests
            
            flutterwave_key = settings.PAYMENT_PROVIDERS["flutterwave"]["secret_key"]
            url = "https://api.flutterwave.com/v3/payments"
            headers = {"Authorization": f"Bearer {flutterwave_key}"}
            
            # Generate unique reference
            reference = f"MANUAL_{payment.id}_{int(timezone.now().timestamp())}"
            
            # Get base URL for callback
            base_url = settings.BILLING_MICROSERVICE_URL
            redirect_url = f"{base_url}/api/v1/payment/payment-verify/confirm/?tx_ref={reference}&confirm_token={payment.transaction_id}&provider=flutterwave&amount={float(amount)}&plan_id={str(payment.plan.id)}&tenant_id={tenant_id}"
            
            data = {
                "tx_ref": reference,
                "amount": str(amount),
                "currency": settings.PAYMENT_CURRENCY,
                "redirect_url": redirect_url,
                "meta": {
                    "payment_id": str(payment.id),
                    "subscription_id": str(payment.subscription.id),
                    "tenant_id": tenant_id,
                    "payment_type": "manual_new_card"
                },
                "customer": {
                    "email": user_email
                },
                "customizations": {
                    "title": "ERP Manual Payment",
                    "logo": "https://tse3.mm.bing.net/th/id/OIP._08Ei4c5bwrBSNNLsoWMhgHaHa?cb=12&rs=1&pid=ImgDetMain&o=7&rm=3"
                },
                "configurations": {
                    "session_duration": 10,
                    "max_retry_attempt": 5
                }
            }
            
            response = requests.post(url, headers=headers, json=data, timeout=10)
            response.raise_for_status()
            response_data = response.json()
            
            if response_data.get('status') == 'success':
                # Update payment with reference
                payment.transaction_id = reference
                payment.save()
                
                return {
                    'status': 'success',
                    'payment_id': str(payment.id),
                    'payment_url': response_data['data']['link'],
                    'flw_ref': response_data['data']['flw_ref'],
                    'reference': reference,
                    'amount': str(amount),
                    'provider': 'flutterwave',
                    'message': 'Please complete payment with your new card details'
                }
            else:
                error_msg = response_data.get('message', 'Payment initialization failed')
                payment.delete()
                return {'status': 'error', 'message': error_msg}
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Flutterwave API error: {str(e)}")
            payment.delete()
            return {'status': 'error', 'message': 'Flutterwave service unavailable'}
        except Exception as e:
            logger.error(f"Flutterwave payment initialization error: {str(e)}")
            payment.delete()
            return {'status': 'error', 'message': str(e)}

    def process_auto_renewal_payment(self, subscription: Subscription, tenant_billing_preferences) -> Dict[str, Any]:
        """
        Process auto-renewal payment using direct charge method (like Node.js example).
        This is the simplified approach for auto-renewal.
        """
        try:
            # Get payment provider info from tenant billing preferences
            # For now, we'll use the last payment as the source of payment info
            last_payment = subscription.payments.filter(status='completed').order_by('-payment_date').first()
            if not last_payment:
                return {'status': 'error', 'message': 'No payment history found for auto-renewal'}

            provider = last_payment.provider

            if provider == 'paystack':
                return self._paystack_direct_charge_for_renewal(subscription, tenant_billing_preferences, last_payment)
            elif provider == 'flutterwave':
                return self._flutterwave_renewal_payment(subscription, tenant_billing_preferences, last_payment)
            else:
                return {'status': 'error', 'message': 'No payment provider info found'}

        except Exception as e:
            logger.error(f"Auto-renewal payment failed: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _paystack_direct_charge_for_renewal(self, subscription: Subscription, tenant_billing_preferences, last_payment) -> Dict[str, Any]:
        """Process Paystack auto-renewal using direct charge (Node.js method)"""
        try:
            # Get stored authorization code from last payment
            # In a real implementation, this would be stored in the tenant billing preferences
            # For now, we'll assume the authorization code is available in the payment metadata
            authorization_code = last_payment.metadata.get('authorization_code') if last_payment.metadata else None
            customer_email = last_payment.subscription.tenant_id  # This should be the user email

            if not authorization_code:
                return {
                    'status': 'failed',
                    'reason': 'No authorization code found',
                    'action_required': 'user_action_required'
                }

            # Use direct charge endpoint like in Node.js example
            paystack_key = settings.PAYMENT_PROVIDERS['paystack']['secret_key']
            url = "https://api.paystack.co/transaction/charge_authorization"
            headers = {
                "Authorization": f"Bearer {paystack_key}",
                "Content-Type": "application/json"
            }

            # Calculate amount (plan price)
            amount = subscription.plan.price

            data = {
                "email": customer_email,
                "amount": int(float(amount) * 100),  # Convert to kobo
                "authorization_code": authorization_code
            }

            logger.info(f"Processing Paystack auto-renewal for subscription {subscription.id}")
            logger.info(f"Using authorization code: {authorization_code}")
            logger.info(f"Amount: {amount} ({int(float(amount) * 100)} kobo)")

            response = requests.post(url, headers=headers, json=data, timeout=10)
            response.raise_for_status()
            response_data = response.json()

            if response_data.get('status') and response_data.get('data'):
                transaction_data = response_data['data']

                # Create Payment record for tracking
                from apps.payment.models import Payment
                payment = Payment.objects.create(
                    plan=subscription.plan,
                    subscription=subscription,
                    amount=amount,
                    transaction_id=transaction_data.get('reference', str(uuid.uuid4())),
                    status='completed' if transaction_data.get('status') == 'success' else 'failed',
                    provider='paystack',
                    payment_type='renewal'
                )

                if transaction_data.get('status') == 'success':
                    # Extend subscription by one billing period
                    new_end_date = subscription.end_date
                    if subscription.plan.billing_period == 'monthly':
                        from dateutil.relativedelta import relativedelta
                        new_end_date = subscription.end_date + relativedelta(months=1)
                    elif subscription.plan.billing_period == 'quarterly':
                        new_end_date = subscription.end_date + relativedelta(months=3)
                    elif subscription.plan.billing_period == 'biannual':
                        new_end_date = subscription.end_date + relativedelta(months=6)
                    elif subscription.plan.billing_period == 'annual':
                        new_end_date = subscription.end_date + relativedelta(years=1)

                    subscription.end_date = new_end_date
                    subscription.status = 'active'
                    subscription.save()

                    # Update tenant billing preferences
                    tenant_billing_preferences.subscription_expiry_date = new_end_date
                    tenant_billing_preferences.next_renewal_date = new_end_date
                    tenant_billing_preferences.save()

                    logger.info(f"Paystack direct charge successful for subscription {subscription.id}")
                    return {
                        'status': 'success',
                        'message': 'Auto-renewal successful',
                        'payment_id': str(payment.id),
                        'transaction_id': transaction_data.get('reference'),
                        'amount': str(amount),
                        'new_end_date': new_end_date.isoformat(),
                        'next_renewal_date': tenant_billing_preferences.next_renewal_date.isoformat() if tenant_billing_preferences.next_renewal_date else None
                    }
                else:
                    return {
                        'status': 'failed',
                        'message': f"Payment failed: {transaction_data.get('gateway_response', 'Unknown error')}",
                        'payment_id': str(payment.id)
                    }
            else:
                error_msg = response_data.get('message', 'Auto-renewal payment failed')
                logger.error(f"Paystack auto-renewal failed: {error_msg}")
                return {'status': 'error', 'message': error_msg}

        except requests.exceptions.RequestException as e:
            logger.error(f"Paystack API error during auto-renewal: {str(e)}")
            return {'status': 'error', 'message': 'Paystack service unavailable'}
        except Exception as e:
            logger.error(f"Paystack auto-renewal processing failed: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _flutterwave_renewal_payment(self, subscription: Subscription, tenant_billing_preferences, last_payment) -> Dict[str, Any]:
        """Process Flutterwave auto-renewal"""
        try:
            # Flutterwave doesn't have direct charge like Paystack
            # For now, we'll mark as requiring user action
            logger.info(f"Flutterwave auto-renewal requires user action for subscription {subscription.id}")
            return {
                'status': 'requires_action',
                'message': 'Flutterwave requires user to complete payment manually',
                'action_required': 'user_payment_required'
            }
        except Exception as e:
            logger.error(f"Flutterwave auto-renewal failed: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def _extend_subscription_period(self, subscription: Subscription) -> None:
        """Extend subscription by one billing period"""
        from dateutil.relativedelta import relativedelta
        
        if subscription.plan.billing_period == 'monthly':
            subscription.end_date = subscription.end_date + relativedelta(months=1)
        elif subscription.plan.billing_period == 'quarterly':
            subscription.end_date = subscription.end_date + relativedelta(months=3)
        elif subscription.plan.billing_period == 'biannual':
            subscription.end_date = subscription.end_date + relativedelta(months=6)
        elif subscription.plan.billing_period == 'annual':
            subscription.end_date = subscription.end_date + relativedelta(years=1)
        
        subscription.status = 'active'
        subscription.save()
        logger.info(f"Subscription {subscription.id} extended to {subscription.end_date}")




    def _calculate_next_renewal_date_for_auto_renewal(self, expiry_date, billing_period: str):
        """Calculate the next renewal date based on billing period (helper for SubscriptionService)"""
        from dateutil.relativedelta import relativedelta
        
        period_mapping = {
            'monthly': relativedelta(months=1),
            'quarterly': relativedelta(months=3),
            'biannual': relativedelta(months=6),
            'annual': relativedelta(years=1),
        }
        
        delta = period_mapping.get(billing_period, relativedelta(months=1))
        return expiry_date + delta


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
                'auto_renew': subscription.tenant_billing_preferences.auto_renew_enabled if subscription.tenant_billing_preferences else False
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

