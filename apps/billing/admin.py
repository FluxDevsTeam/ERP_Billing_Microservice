from django.contrib import admin
from .models import Plan, Subscription, RecurringToken

admin.site.register(Plan)
admin.site.register(Subscription)

@admin.register(RecurringToken)
class RecurringTokenAdmin(admin.ModelAdmin):
    list_display = [
        'subscription',
        'provider', 'last4', 'card_brand', 'email', 'is_active', 'created_at', 'updated_at'
    ]
    search_fields = ['subscription__tenant_id', 'email', 'last4', 'card_brand']
    list_filter = ['provider', 'is_active', 'created_at', 'updated_at']
    readonly_fields = ['created_at', 'updated_at']

