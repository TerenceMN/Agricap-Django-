"""Admin FX en LECTURE SEULE.

Un taux est probant (principe 3) : il se publie et se valide par les services (qui
journalisent, contrôlent l'écart et imposent maker ≠ checker), jamais par un formulaire
d'administration qui court-circuiterait ces trois contrôles. L'admin sert donc à consulter
l'historique, pas à le réécrire.
"""
from django.contrib import admin

from .models import ExchangeRate


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ("effective_date", "tier", "currency", "usage", "status", "version",
                    "buy_rate", "sell_rate", "source", "variation_pct", "created_by",
                    "validated_by")
    list_filter = ("tier", "currency", "usage", "status", "source")
    search_fields = ("created_by", "validated_by", "source_reference", "reason")
    date_hierarchy = "effective_date"
    ordering = ("-effective_date", "tier", "currency")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
