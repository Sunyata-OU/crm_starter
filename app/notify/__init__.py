"""Notifications.

A notification is a stored record; delivering it to email, chat or anywhere
else is a separate, pluggable step. That split means the in-app bell always
works, and a failing SMTP server loses nothing.

Importing this package registers the built-in channels.
"""

from __future__ import annotations

from app.notify.base import (  # noqa: F401
    BaseChannel,
    Channel,
    Delivery,
    Kind,
    Notification,
    Priority,
    assigned,
    build_channel,
    due,
    overdue,
    register_channel,
    registered_channels,
    reminder_for,
)
from app.notify.channels import (  # noqa: F401
    ConsoleChannel,
    EmailChannel,
    InAppChannel,
    WebhookChannel,
)
from app.notify.service import Notifier, notifier  # noqa: F401

__all__ = [
    "BaseChannel",
    "Channel",
    "ConsoleChannel",
    "Delivery",
    "EmailChannel",
    "InAppChannel",
    "Kind",
    "Notification",
    "Notifier",
    "Priority",
    "WebhookChannel",
    "assigned",
    "build_channel",
    "due",
    "notifier",
    "overdue",
    "register_channel",
    "registered_channels",
    "reminder_for",
]
