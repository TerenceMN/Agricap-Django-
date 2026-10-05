from django.urls import path

from . import views, views_templates

urlpatterns = [
    path("sources", views.sources),
    path("sources/<int:pk>", views.source_detail),
    path("sources/<int:pk>/commit", views.commit_source),
    path("sources/<int:pk>/tables", views.source_tables),
    path("tables/<int:pk>", views.table_detail),
    path("tables/<int:pk>/records", views.update_table_records),
    path("history", views.history),

    # Templates de fichiers versionnés (principe 11) — maker-checker.
    path("templates/", views_templates.templates),
    path("templates/upload", views_templates.upload_template),
    path("templates/<int:pk>", views_templates.template_detail),
    path("templates/<int:pk>/activate", views_templates.activate_template),
]
