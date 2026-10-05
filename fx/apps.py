from django.apps import AppConfig


class FxConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fx"
    verbose_name = "Taux de change (multi-devises CDF/USD/...)"
