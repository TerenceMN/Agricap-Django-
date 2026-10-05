"""Boîte de notifications par utilisateur (ClientNotifications.jsx /
InvestorNotifications.jsx)."""
from __future__ import annotations

from django.db import models


class Notification(models.Model):
    user = models.ForeignKey("accounts.FintechUser", on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
