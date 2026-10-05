from django.contrib import admin

from .models import FileTemplate


@admin.register(FileTemplate)
class FileTemplateAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "version", "status", "original_name",
                    "uploaded_by", "activated_by", "activated_at")
    list_filter = ("kind", "status")
    search_fields = ("original_name", "sha256", "uploaded_by", "activated_by")
    readonly_fields = ("sha256", "schema", "uploaded_at", "activated_at")
