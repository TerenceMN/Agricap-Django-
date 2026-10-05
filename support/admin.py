from django.contrib import admin

from .models import (
    MobileMoneyVerification, PendingFinancialAction, Ticket, TicketAuditLog, TicketMessage,
)


class TicketMessageInline(admin.TabularInline):
    model = TicketMessage
    extra = 0
    readonly_fields = ("author_sub", "author_role", "text", "is_internal", "created_at")
    can_delete = False


class TicketAuditLogInline(admin.TabularInline):
    model = TicketAuditLog
    extra = 0
    readonly_fields = ("actor", "action", "payload", "ip_address", "created_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("pk", "public_id", "subject", "category", "priority", "status",
                    "level", "assigned_to_sub", "created_at")
    list_filter = ("status", "category", "priority", "level")
    search_fields = ("subject", "description", "user__email", "user__full_name")
    readonly_fields = ("created_at", "public_id", "first_response_at", "resolved_at")
    inlines = [TicketMessageInline, TicketAuditLogInline]

    def public_id(self, obj):
        return obj.public_id
    public_id.short_description = "ID public"


@admin.register(TicketAuditLog)
class TicketAuditLogAdmin(admin.ModelAdmin):
    list_display = ("ticket_id", "actor", "action", "ip_address", "created_at")
    list_filter = ("action",)
    readonly_fields = ("ticket", "actor", "action", "payload", "ip_address", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MobileMoneyVerification)
class MobileMoneyVerificationAdmin(admin.ModelAdmin):
    list_display = ("ticket_id", "operator", "transaction_ref", "status", "amount", "currency", "created_at")
    list_filter = ("operator", "status")
    readonly_fields = ("ticket", "operator", "transaction_ref", "amount", "currency",
                       "status", "raw_response", "verified_at", "created_at")


@admin.register(PendingFinancialAction)
class PendingFinancialActionAdmin(admin.ModelAdmin):
    list_display = ("pk", "ticket_id", "action_type", "amount", "currency",
                    "initiated_by", "status", "created_at")
    list_filter = ("status", "action_type")
    readonly_fields = ("ticket", "action_type", "amount", "currency", "initiated_by",
                       "idempotency_key", "created_at")
