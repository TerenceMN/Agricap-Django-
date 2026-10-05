from django.contrib import admin

from .models import CaisseConfig


@admin.register(CaisseConfig)
class CaisseConfigAdmin(admin.ModelAdmin):
    """Surface d'édition du comité (principe 8) : régler la tolérance d'écart de caisse sans
    redéploiement. L'enregistrement le plus récent fait foi (`cash_register.discrepancy_tolerance`)."""
    list_display = ("id", "discrepancy_tolerance", "updated_by", "updated_at")
    readonly_fields = ("created_at", "updated_at")
