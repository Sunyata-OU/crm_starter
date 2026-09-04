"""Where notifications go.

Three shipped: the in-app bell, email, and a webhook that covers Slack, Teams
and anything else that accepts a POST. Adding another is a decorator.
"""

from __future__ import annotations

import json
import logging
from email.message import EmailMessage
from typing import Any

from app.notify.base import (
    BaseChannel,
    Delivery,
    Notification,
    Priority,
    register_channel,
    with_retries,
)

log = logging.getLogger("crm.notify")


class InAppChannel(BaseChannel):
    """The bell in the header.

    Nothing to deliver: the notification row *is* the delivery. It exists as a
    channel so the set of channels is uniform, and so the bell can be turned
    off per notification like anything else.
    """

    name = "inapp"

    async def send(self, notification: Notification) -> Delivery:
        return Delivery(self.name, True, "stored")

    async def health(self) -> tuple[bool, str]:
        return True, "in-app notifications are stored with the record"


@register_channel("inapp")
def build_inapp(**options: Any) -> InAppChannel:
    return InAppChannel()


class EmailChannel(BaseChannel):
    """Sends over SMTP.

    Defaults to `high` and above, because a CRM generates a great many
    notifications and emailing all of them is how people learn to filter you
    into a folder they never read.
    """

    name = "email"

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 25,
        username: str = "",
        password: str = "",
        use_tls: bool = False,
        sender: str = "crm@localhost",
        base_url: str = "",
        min_priority: str = "high",
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.sender = sender
        self.base_url = base_url.rstrip("/")
        self.min_priority = Priority(min_priority)
        self.timeout = timeout

    def build_message(self, notification: Notification) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = notification.title
        message["From"] = self.sender
        message["To"] = notification.recipient

        lines = [notification.title, ""]
        if notification.body:
            lines += [notification.body, ""]
        if notification.url:
            lines.append(f"{self.base_url}{notification.url}")
        message.set_content("\n".join(lines))
        return message

    async def send(self, notification: Notification) -> Delivery:
        if "@" not in notification.recipient:
            # Recipients are identities, which are usually emails but need not
            # be. Nothing to do rather than an error.
            return Delivery(self.name, False, "the recipient is not an email address")

        import asyncio
        import smtplib

        message = self.build_message(notification)

        def deliver() -> None:
            server_class = smtplib.SMTP_SSL if self.port == 465 else smtplib.SMTP
            with server_class(self.host, self.port, timeout=self.timeout) as server:
                if self.use_tls and self.port != 465:
                    server.starttls()
                if self.username:
                    server.login(self.username, self.password)
                server.send_message(message)

        async def attempt() -> Delivery:
            try:
                # smtplib is synchronous; a thread keeps it off the event loop.
                await asyncio.to_thread(deliver)
            except smtplib.SMTPResponseException as exc:
                # 4xx is "try later" in SMTP; 5xx is "never". Honouring the
                # distinction is the difference between retrying a busy server
                # and retrying a rejected address three times.
                return Delivery(
                    self.name, False, f"SMTP {exc.smtp_code}: {exc.smtp_error!r}",
                    transient=400 <= exc.smtp_code < 500,
                )
            except (OSError, smtplib.SMTPServerDisconnected) as exc:
                return Delivery(
                    self.name, False, f"{type(exc).__name__}: {exc}", transient=True
                )
            except Exception as exc:
                return Delivery(self.name, False, f"{type(exc).__name__}: {exc}")
            return Delivery(self.name, True, f"sent to {notification.recipient}")

        return await with_retries(attempt, channel=self.name)

    async def health(self) -> tuple[bool, str]:
        import asyncio
        import smtplib

        def probe() -> str:
            server_class = smtplib.SMTP_SSL if self.port == 465 else smtplib.SMTP
            with server_class(self.host, self.port, timeout=self.timeout) as server:
                server.noop()
            return f"{self.host}:{self.port} reachable"

        try:
            return True, await asyncio.to_thread(probe)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


@register_channel("email")
def build_email(**options: Any) -> EmailChannel:
    return EmailChannel(**options)


class WebhookChannel(BaseChannel):
    """POSTs a JSON payload.

    Covers Slack and Teams incoming webhooks, and anything else that accepts a
    POST. The payload shape is configurable because no two services agree.
    """

    name = "webhook"

    def __init__(
        self,
        *,
        url: str = "",
        headers: dict[str, str] | None = None,
        #: "slack" produces a `text` field; "raw" sends the notification itself.
        style: str = "raw",
        base_url: str = "",
        min_priority: str = "normal",
        timeout: float = 10.0,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.style = style
        self.base_url = base_url.rstrip("/")
        self.min_priority = Priority(min_priority)
        self.timeout = timeout

    def payload(self, notification: Notification) -> dict[str, Any]:
        link = f"{self.base_url}{notification.url}" if notification.url else ""
        if self.style == "slack":
            text = f"*{notification.title}*"
            if notification.body:
                text += f"\n{notification.body}"
            if link:
                text += f"\n{link}"
            return {"text": text}
        return {
            "title": notification.title,
            "body": notification.body,
            "kind": str(notification.kind),
            "priority": str(notification.priority),
            "recipient": notification.recipient,
            "url": link,
            "resource": notification.resource,
            "record_id": notification.record_id,
        }

    async def send(self, notification: Notification) -> Delivery:
        if not self.url:
            return Delivery(self.name, False, "no webhook URL configured")

        import httpx

        async def attempt() -> Delivery:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        self.url, json=self.payload(notification), headers=self.headers
                    )
            except httpx.HTTPError as exc:
                # A timeout or a refused connection: the other end may simply
                # be busy or restarting.
                return Delivery(
                    self.name, False, f"{type(exc).__name__}: {exc}", transient=True
                )
            except Exception as exc:
                return Delivery(self.name, False, f"{type(exc).__name__}: {exc}")

            if response.status_code >= 400:
                return Delivery(
                    self.name, False, f"HTTP {response.status_code}",
                    # 5xx and 429 are the server's problem and may pass; a 4xx
                    # means this request will be rejected however often it is
                    # sent.
                    transient=response.status_code >= 500 or response.status_code == 429,
                )
            return Delivery(self.name, True, f"HTTP {response.status_code}")

        return await with_retries(attempt, channel=self.name)

    async def health(self) -> tuple[bool, str]:
        if not self.url:
            return False, "no webhook URL configured"
        return True, f"configured for {self.url.split('?')[0]}"


@register_channel("webhook")
def build_webhook(**options: Any) -> WebhookChannel:
    return WebhookChannel(**options)


class ConsoleChannel(BaseChannel):
    """Writes to the log. For development, and as a fallback worth having."""

    name = "console"

    async def send(self, notification: Notification) -> Delivery:
        log.info(
            "notify %s [%s] %s%s",
            notification.recipient,
            notification.kind,
            notification.title,
            f" -> {notification.url}" if notification.url else "",
        )
        return Delivery(self.name, True, "written to the log")


@register_channel("console")
def build_console(**options: Any) -> ConsoleChannel:
    return ConsoleChannel()


def summarise(deliveries: list[Delivery]) -> str:
    """A compact record of what each channel did, for the stored row."""
    return json.dumps(
        {d.channel: {"ok": d.delivered, "detail": d.detail} for d in deliveries}
    )
