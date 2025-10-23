from datetime import datetime
from dateutil.relativedelta import relativedelta
from django.utils import timezone


class PeriodCalculator:
    """Utility class for handling subscription period calculations"""

    @staticmethod
    def get_period_delta(billing_period: str) -> relativedelta:
        """Convert billing period to a relativedelta object"""
        period_mapping = {
            'monthly': relativedelta(months=1),
            'quarterly': relativedelta(months=3),
            'biannual': relativedelta(months=6),
            'annual': relativedelta(years=1),
        }
        return period_mapping.get(billing_period, relativedelta(months=1))

    @staticmethod
    def calculate_end_date(start_date: datetime, billing_period: str) -> datetime:
        """Calculate the end date based on the billing period"""
        delta = PeriodCalculator.get_period_delta(billing_period)
        # Add the period and subtract one day to get the last day of the period
        end_date = start_date + delta - relativedelta(days=1)
        return end_date

    @staticmethod
    def calculate_next_period_start(current_end_date: datetime) -> datetime:
        """Calculate the start date of the next period"""
        return current_end_date + relativedelta(days=1)

    @staticmethod
    def get_period_display(billing_period: str, start_date: datetime = None) -> dict:
        """Get human-readable period information"""
        if not start_date:
            start_date = timezone.now()

        end_date = PeriodCalculator.calculate_end_date(start_date, billing_period)

        return {
            'period': billing_period,
            'start_date': start_date,
            'end_date': end_date,
            'duration': {
                'months': PeriodCalculator.get_period_delta(billing_period).months,
                'years': PeriodCalculator.get_period_delta(billing_period).years,
            }
        }

    @staticmethod
    def is_valid_period(billing_period: str) -> bool:
        """Check if the billing period is valid"""
        return billing_period in ['monthly', 'quarterly', 'biannual', 'annual']
