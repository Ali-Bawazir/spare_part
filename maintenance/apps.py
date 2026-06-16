from django.apps import AppConfig


class MaintenanceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'maintenance'
    verbose_name = 'Maintenance'

    def ready(self):
        # Register the blocker-system signal handlers (see signals.py).
        from . import signals  # noqa: F401
