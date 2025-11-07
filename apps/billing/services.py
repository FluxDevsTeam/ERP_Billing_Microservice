from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.conf import settings
import uuid
import logging
from typing import Optional, Dict, Any, Tuple
from decimal import Decimal

from .models import Plan, Subscription, AuditLog, SubscriptionCredit, AutoRenewal, TrialUsage
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
    def create_subscription(self, tenant_id: str, plan_id: str, user: str = None, machine_number: str = None, is_trial: bool = False) -> Tuple[Subscription, Dict[str, Any]]:
        """
        Create a new subscription. If there's a previous subscription (trial or expired),
        carry over remaining days to the new subscription.
        """
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
            # Check machine number for trial abuse prevention
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
            end_date=None,  # Let save() calculate it first
            next_payment_date=trial_end_date if is_trial else None,
            is_first_time_subscription=not previous_subscription,
            trial_used=is_trial
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

            # Update auto-renewal if it exists
            try:
                auto_renewal = AutoRenewal.objects.filter(
                    subscription=subscription,
                    status='active'
                ).first()
                if auto_renewal:
                    auto_renewal.expiry_date = subscription.end_date
                    auto_renewal.next_renewal_date = self._calculate_next_renewal_date_for_auto_renewal(
                        subscription.end_date,
                        subscription.plan.billing_period
                    )
                    auto_renewal.last_renewal_at = timezone.now()
                    auto_renewal.save()
            except Exception as e:
                logger.warning(f"Failed to update auto-renewal after subscription renewal: {str(e)}")

            self._audit_log(subscription, 'renewed', user, {
                'new_end_date': subscription.end_date.isoformat(),
                'plan_name': subscription.plan.name,
                'billing_period': subscription.plan.billing_period
            })

            return subscription, {'status': 'success', 'new_end_date': subscription.end_date.isoformat()}

        except Subscription.DoesNotExist:
            raise ValidationError("Subscription not found")
        except Exception as e:
            raise ValidationError(f"Subscription renewal failed: {str(e)}")

    @transaction.atomic
    def toggle_auto_renew(self, subscription_id: str, auto_renew: bool, user: str = None) -> Tuple[Subscription, Dict[str, Any]]:
        """
        Toggle auto-renewal on/off. This does NOT cancel the subscription.
        Only enables/disables automatic renewal. Subscription status remains unchanged.
        """
        try:
            subscription = Subscription.objects.get(id=subscription_id)
            # Allow toggling for any status except canceled/suspended
            # Toggling auto-renew should never cancel the subscription
            if subscription.status in ['canceled', 'suspended']:
                raise ValidationError("Cannot toggle auto-renew for canceled or suspended subscription")

            subscription.auto_renew = auto_renew

            self._audit_log(subscription, 'auto_renew_toggled', user, {
                'auto_renew': auto_renew,
                'subscription_status': subscription.status,
                'tenant_id': str(subscription.tenant_id),
                'note': 'Auto-renew toggled without affecting subscription status'
            })
            return subscription, {'status': 'success', 'auto_renew': auto_renew}

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


class AutoRenewalService:
    """Service for managing auto-renewals"""

    def __init__(self, request=None):
        self.request = request
        self.subscription_service = SubscriptionService(request)

    @transaction.atomic
    def create_auto_renewal(self, tenant_id: str, plan_id: str, expiry_date, 
                           user_id: str = None, subscription_id: str = None) -> Tuple[AutoRenewal, Dict[str, Any]]:
        """Create a new auto-renewal"""
        try:
            tenant_uuid = uuid.UUID(tenant_id)
            plan_uuid = uuid.UUID(plan_id)
        except ValueError:
            logger.error(f"Auto-renewal creation failed: Invalid tenant_id {tenant_id} or plan_id {plan_id}")
            raise ValidationError("Invalid tenant_id or plan_id format")

        plan = Plan.objects.get(id=plan_uuid)
        if not plan.is_active or plan.discontinued:
            logger.warning(f"Auto-renewal creation failed: Plan {plan_id} is not available")
            raise ValidationError("Plan is not available")

        subscription = None
        if subscription_id:
            try:
                subscription = Subscription.objects.get(id=subscription_id)
            except Subscription.DoesNotExist:
                logger.warning(f"Subscription {subscription_id} not found, creating auto-renewal without subscription reference")

        # Check for existing auto-renewal
        existing = AutoRenewal.objects.filter(tenant_id=tenant_uuid, plan=plan, status='active').first()
        if existing:
            logger.warning(f"Auto-renewal already exists for tenant {tenant_id} and plan {plan_id}")
            raise ValidationError("Active auto-renewal already exists for this tenant and plan")

        # Calculate next renewal date
        next_renewal_date = self._calculate_next_renewal_date(expiry_date, plan.billing_period)

        auto_renewal = AutoRenewal.objects.create(
            tenant_id=tenant_uuid,
            user_id=user_id,
            subscription=subscription,
            plan=plan,
            expiry_date=expiry_date,
            next_renewal_date=next_renewal_date,
            status='active'
        )

        logger.info(f"Auto-renewal created for tenant {tenant_id} with plan {plan.name}")
        return auto_renewal, {
            'status': 'success',
            'auto_renewal_id': str(auto_renewal.id),
            'next_renewal_date': next_renewal_date.isoformat()
        }

    @transaction.atomic
    def process_auto_renewal(self, auto_renewal_id: str) -> Dict[str, Any]:
        """Process a single auto-renewal"""
        try:
            auto_renewal = AutoRenewal.objects.select_related('plan', 'subscription').get(id=auto_renewal_id)
            
            if not auto_renewal.is_due_for_renewal():
                return {
                    'status': 'skipped',
                    'message': 'Auto-renewal is not due yet',
                    'next_renewal_date': auto_renewal.next_renewal_date.isoformat() if auto_renewal.next_renewal_date else None
                }

            can_renew, reason = auto_renewal.can_renew()
            if not can_renew:
                # Handle deprecated plan
                if auto_renewal.plan.discontinued or not auto_renewal.plan.is_active:
                    return self._handle_deprecated_plan(auto_renewal)
                
                # Increment failure count
                auto_renewal.failure_count += 1
                auto_renewal.status = 'failed' if auto_renewal.failure_count >= auto_renewal.max_failures else 'active'
                auto_renewal.save()
                
                logger.warning(f"Auto-renewal {auto_renewal_id} cannot be processed: {reason}")
                return {
                    'status': 'failed',
                    'message': reason,
                    'failure_count': auto_renewal.failure_count
                }

            # Mark as processing
            auto_renewal.status = 'processing'
            auto_renewal.save()

            try:
                # Get or create subscription
                subscription = auto_renewal.subscription
                if not subscription:
                    subscription = Subscription.objects.filter(tenant_id=auto_renewal.tenant_id).first()
                
                if not subscription:
                    raise ValidationError("No subscription found for auto-renewal")

                # Renew the subscription
                subscription, result = self.subscription_service.renew_subscription(
                    subscription_id=str(subscription.id),
                    user=auto_renewal.user_id or 'system'
                )

                # Update auto-renewal
                auto_renewal.status = 'active'
                auto_renewal.last_renewal_at = timezone.now()
                auto_renewal.failure_count = 0
                auto_renewal.expiry_date = subscription.end_date
                auto_renewal.next_renewal_date = self._calculate_next_renewal_date(
                    subscription.end_date, 
                    auto_renewal.plan.billing_period
                )
                auto_renewal.subscription = subscription
                auto_renewal.save()

                # Log audit
                if subscription:
                    self.subscription_service._audit_log(
                        subscription,
                        'auto_renew_processed',
                        auto_renewal.user_id or 'system',
                        {
                            'auto_renewal_id': str(auto_renewal.id),
                            'plan_name': auto_renewal.plan.name,
                            'next_renewal_date': auto_renewal.next_renewal_date.isoformat()
                        }
                    )

                logger.info(f"Auto-renewal {auto_renewal_id} processed successfully")
                return {
                    'status': 'success',
                    'auto_renewal_id': str(auto_renewal.id),
                    'subscription_id': str(subscription.id),
                    'next_renewal_date': auto_renewal.next_renewal_date.isoformat()
                }

            except Exception as e:
                # Handle renewal failure
                auto_renewal.failure_count += 1
                auto_renewal.status = 'failed' if auto_renewal.failure_count >= auto_renewal.max_failures else 'active'
                auto_renewal.save()

                logger.error(f"Auto-renewal {auto_renewal_id} processing failed: {str(e)}")
                
                if subscription:
                    self.subscription_service._audit_log(
                        subscription,
                        'auto_renew_failed',
                        auto_renewal.user_id or 'system',
                        {
                            'auto_renewal_id': str(auto_renewal.id),
                            'error': str(e),
                            'failure_count': auto_renewal.failure_count
                        }
                    )

                return {
                    'status': 'error',
                    'message': str(e),
                    'failure_count': auto_renewal.failure_count
                }

        except AutoRenewal.DoesNotExist:
            logger.error(f"Auto-renewal {auto_renewal_id} not found")
            raise ValidationError("Auto-renewal not found")
        except Exception as e:
            logger.error(f"Auto-renewal processing failed: {str(e)}")
            raise ValidationError(f"Auto-renewal processing failed: {str(e)}")

    @transaction.atomic
    def process_due_auto_renewals(self) -> Dict[str, Any]:
        """Process all due auto-renewals"""
        try:
            due_renewals = AutoRenewal.objects.filter(
                status='active',
                next_renewal_date__lte=timezone.now()
            ).select_related('plan', 'subscription')

            processed = 0
            succeeded = 0
            failed = 0
            skipped = 0

            for auto_renewal in due_renewals:
                result = self.process_auto_renewal(str(auto_renewal.id))
                processed += 1
                
                if result['status'] == 'success':
                    succeeded += 1
                elif result['status'] == 'failed' or result['status'] == 'error':
                    failed += 1
                else:
                    skipped += 1

            logger.info(f"Processed {processed} auto-renewals: {succeeded} succeeded, {failed} failed, {skipped} skipped")
            return {
                'status': 'success',
                'processed': processed,
                'succeeded': succeeded,
                'failed': failed,
                'skipped': skipped
            }

        except Exception as e:
            logger.error(f"Processing due auto-renewals failed: {str(e)}")
            return {
                'status': 'error',
                'message': str(e)
            }

    def _handle_deprecated_plan(self, auto_renewal: AutoRenewal) -> Dict[str, Any]:
        """Handle auto-renewal when plan is deprecated"""
        try:
            # Try to find an alternative plan
            alternative_plan = self._find_alternative_plan(auto_renewal.plan, auto_renewal.tenant_id)
            
            if alternative_plan:
                # Update auto-renewal to use alternative plan
                old_plan = auto_renewal.plan
                auto_renewal.plan = alternative_plan
                auto_renewal.notes = f"Plan changed from {old_plan.name} (deprecated) to {alternative_plan.name}"
                auto_renewal.save()

                logger.info(f"Auto-renewal {auto_renewal.id} updated to alternative plan {alternative_plan.name}")

                # Log audit
                if auto_renewal.subscription:
                    self.subscription_service._audit_log(
                        auto_renewal.subscription,
                        'plan_deprecated_handled',
                        auto_renewal.user_id or 'system',
                        {
                            'auto_renewal_id': str(auto_renewal.id),
                            'old_plan_id': str(old_plan.id),
                            'old_plan_name': old_plan.name,
                            'new_plan_id': str(alternative_plan.id),
                            'new_plan_name': alternative_plan.name,
                            'reason': 'Plan deprecated'
                        }
                    )

                # Try to process again with new plan
                return self.process_auto_renewal(str(auto_renewal.id))
            else:
                # No alternative found, pause the auto-renewal
                auto_renewal.status = 'paused'
                auto_renewal.notes = f"Auto-renewal paused: Plan {auto_renewal.plan.name} is deprecated and no alternative found"
                auto_renewal.save()

                logger.warning(f"Auto-renewal {auto_renewal.id} paused: No alternative plan found for deprecated plan {auto_renewal.plan.name}")

                return {
                    'status': 'paused',
                    'message': 'Plan is deprecated and no alternative plan found',
                    'auto_renewal_id': str(auto_renewal.id)
                }

        except Exception as e:
            logger.error(f"Error handling deprecated plan for auto-renewal {auto_renewal.id}: {str(e)}")
            return {
                'status': 'error',
                'message': f"Error handling deprecated plan: {str(e)}"
            }

    def _find_alternative_plan(self, deprecated_plan: Plan, tenant_id: str) -> Optional[Plan]:
        """Find an alternative plan when the current plan is deprecated"""
        try:
            # Get tenant industry if possible
            tenant_industry = None
            if self.request:
                try:
                    client = IdentityServiceClient(request=self.request)
                    tenant = client.get_tenant(tenant_id=str(tenant_id))
                    tenant_industry = tenant.get('industry') if tenant and isinstance(tenant, dict) else None
                except Exception as e:
                    logger.warning(f"Could not fetch tenant industry: {str(e)}")

            # Find similar plan in same industry and tier
            alternative_plans = Plan.objects.filter(
                is_active=True,
                discontinued=False,
                tier_level=deprecated_plan.tier_level
            )

            if tenant_industry:
                # Prefer same industry
                same_industry = alternative_plans.filter(industry=tenant_industry).exclude(id=deprecated_plan.id).first()
                if same_industry:
                    return same_industry
                # Fallback to 'Other' industry
                other_industry = alternative_plans.filter(industry='Other').exclude(id=deprecated_plan.id).first()
                if other_industry:
                    return other_industry
            else:
                # No industry info, find any similar plan
                alternative = alternative_plans.exclude(id=deprecated_plan.id).first()
                if alternative:
                    return alternative

            return None

        except Exception as e:
            logger.error(f"Error finding alternative plan: {str(e)}")
            return None

    def _calculate_next_renewal_date(self, expiry_date, billing_period: str):
        """Calculate the next renewal date based on billing period"""
        from dateutil.relativedelta import relativedelta
        
        period_mapping = {
            'monthly': relativedelta(months=1),
            'quarterly': relativedelta(months=3),
            'biannual': relativedelta(months=6),
            'annual': relativedelta(years=1),
        }
        
        delta = period_mapping.get(billing_period, relativedelta(months=1))
        return expiry_date + delta

    @transaction.atomic
    def update_auto_renewal(self, auto_renewal_id: str, plan_id: str = None, 
                           status: str = None, user_id: str = None) -> Tuple[AutoRenewal, Dict[str, Any]]:
        """Update an auto-renewal"""
        try:
            auto_renewal = AutoRenewal.objects.get(id=auto_renewal_id)
            
            if plan_id:
                plan = Plan.objects.get(id=plan_id)
                if not plan.is_active or plan.discontinued:
                    raise ValidationError("Plan is not available")
                auto_renewal.plan = plan
                # Recalculate next renewal date
                auto_renewal.next_renewal_date = self._calculate_next_renewal_date(
                    auto_renewal.expiry_date,
                    plan.billing_period
                )
            
            if status:
                if status not in [choice[0] for choice in AutoRenewal.STATUS_CHOICES]:
                    raise ValidationError(f"Invalid status: {status}")
                auto_renewal.status = status
            
            if user_id:
                auto_renewal.user_id = user_id
            
            auto_renewal.save()

            logger.info(f"Auto-renewal {auto_renewal_id} updated")
            return auto_renewal, {
                'status': 'success',
                'auto_renewal_id': str(auto_renewal.id)
            }

        except (AutoRenewal.DoesNotExist, Plan.DoesNotExist):
            raise ValidationError("Auto-renewal or plan not found")
        except Exception as e:
            logger.error(f"Auto-renewal update failed: {str(e)}")
            raise ValidationError(f"Auto-renewal update failed: {str(e)}")

    @transaction.atomic
    def cancel_auto_renewal(self, auto_renewal_id: str, user_id: str = None) -> Tuple[AutoRenewal, Dict[str, Any]]:
        """Cancel an auto-renewal"""
        try:
            auto_renewal = AutoRenewal.objects.get(id=auto_renewal_id)
            auto_renewal.status = 'canceled'
            auto_renewal.save()

            # Log audit
            if auto_renewal.subscription:
                self.subscription_service._audit_log(
                    auto_renewal.subscription,
                    'auto_renew_toggled',
                    user_id or 'system',
                    {
                        'auto_renewal_id': str(auto_renewal.id),
                        'action': 'canceled',
                        'plan_name': auto_renewal.plan.name if auto_renewal.plan else None
                    }
                )

            logger.info(f"Auto-renewal {auto_renewal_id} canceled")
            return auto_renewal, {
                'status': 'success',
                'auto_renewal_id': str(auto_renewal.id)
            }

        except AutoRenewal.DoesNotExist:
            raise ValidationError("Auto-renewal not found")
        except Exception as e:
            logger.error(f"Auto-renewal cancellation failed: {str(e)}")
            raise ValidationError(f"Auto-renewal cancellation failed: {str(e)}")