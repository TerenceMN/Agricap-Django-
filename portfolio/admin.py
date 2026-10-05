from django.contrib import admin

from .models import Loan, LoanConfigHistory, LoanNote, LoanTransaction


class TransactionInline(admin.TabularInline):
    model = LoanTransaction
    extra = 0


@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = ("reference", "operator", "category", "amount_approved", "currency",
                    "status", "manager", "due_date")
    list_filter = ("status", "currency", "category")
    search_fields = ("reference", "operator", "manager", "investor")
    inlines = [TransactionInline]


admin.site.register(LoanConfigHistory)
admin.site.register(LoanNote)
