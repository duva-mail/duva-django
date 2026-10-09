"""Django email backend for Duva: `duva_django.EmailBackend`.

Duva refuses a few things Django allows, and this backend translates rather than failing late: a
display name Duva would reject is dropped (the address is kept), and only the headers Duva accepts
are forwarded. `tags` and `metadata` attributes on the message (the django-anymail convention)
become Duva's `tags` and `metadata`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from email.message import Message
from email.utils import parseaddr
from typing import Any

import duva
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.core.mail.backends.base import BaseEmailBackend

# Duva headers allowlist (docs/api.md); any other `X-*` is allowed except the reserved prefixes.
_ALLOWED_HEADERS = frozenset(
    {
        "list-unsubscribe",
        "list-unsubscribe-post",
        "list-id",
        "in-reply-to",
        "references",
        "auto-submitted",
        "precedence",
        "importance",
        "feedback-id",
    }
)
_RESERVED_PREFIXES = ("x-kumo", "x-duva", "x-tenant", "x-campaign")
# A name Duva refuses: `@`, quotes, angle brackets, backslash, control characters, encoded words.
_UNSAFE_NAME = re.compile(r'[@"<>\\\x00-\x1f\x7f]|=\?')

#: Read by this backend, never forwarded: a stable key so a retried task sends only once.
IDEMPOTENCY_HEADER = "X-Idempotency-Key"


class EmailBackend(BaseEmailBackend):
    """Sends `EmailMessage` objects through the Duva API.

    Configuration, in order of precedence: the options given to the backend (`OPTIONS` of a
    `MAILERS` entry, Django 6.1+), then the settings `DUVA_API_KEY`, `DUVA_DOMAIN`, `DUVA_BASE_URL`,
    `DUVA_TIMEOUT`, `DUVA_MAX_RETRIES`, then the environment variables `DUVA_API_KEY` and
    `DUVA_DOMAIN`. The options are `api_key`, `domain`, `base_url`, `timeout`, `max_retries`.

    After a successful send, `message.duva_result` holds the `duva.SendMessageResult`
    (`id`, `status`, `replayed`).
    """

    def __init__(
        self,
        fail_silently: bool = False,
        *,
        alias: str | None = None,
        api_key: str | None = None,
        domain: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        client: duva.Duva | None = None,
        **kwargs: Any,
    ) -> None:
        # `alias` exists from Django 6.1 only, and `fail_silently` is deprecated there as a base
        # argument: this backend keeps its own attribute.
        extra: dict[str, Any] = {"alias": alias} if alias is not None else {}
        super().__init__(**extra, **kwargs)
        self.fail_silently = fail_silently
        self._options = {
            "api_key": api_key,
            "domain": domain,
            "base_url": base_url,
            "timeout": timeout,
            "max_retries": max_retries,
        }
        self._client = client
        self._owns_client = client is None

    def open(self) -> bool:
        if self._client is not None:
            return False
        options = self._options

        def pick(name: str, setting: str, default: Any = None) -> Any:
            value = options[name]
            return value if value is not None else getattr(settings, setting, default)

        self._client = duva.Duva(
            api_key=pick("api_key", "DUVA_API_KEY"),
            domain=pick("domain", "DUVA_DOMAIN"),
            base_url=pick("base_url", "DUVA_BASE_URL", "https://api.duva.ca"),
            timeout=float(pick("timeout", "DUVA_TIMEOUT", 10.0)),
            max_retries=int(pick("max_retries", "DUVA_MAX_RETRIES", 2)),
        )
        return True

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        if not email_messages:
            return 0
        opened = False
        try:
            if self._client is None:
                try:
                    opened = self.open()
                except (TypeError, ValueError) as error:
                    # Missing key or domain: a configuration error, not a delivery failure.
                    if not self.fail_silently:
                        raise ImproperlyConfigured(str(error)) from error
                    return 0
            sent = 0
            for message in email_messages:
                if self._send(message):
                    sent += 1
            return sent
        finally:
            if opened:
                self.close()

    def _send(self, message: EmailMessage) -> bool:
        assert self._client is not None
        try:
            params = _params(message)
            result = self._client.messages.send(**params)
        except (duva.DuvaError, ValueError):
            if not self.fail_silently:
                raise
            return False
        message.duva_result = result  # type: ignore[attr-defined]
        return True


def _params(message: EmailMessage) -> dict[str, Any]:
    to = _addresses(message.to)
    cc = _addresses(message.cc)
    bcc = _addresses(message.bcc)
    if not (to or cc or bcc):
        raise ValueError("Duva: the message has no recipient")
    if not message.from_email:
        raise ValueError("Duva: the message has no from address")

    params: dict[str, Any] = {
        "from_": _address(message.from_email),
        # Duva requires `to`; a message with only cc/bcc is not accepted by the API either.
        "to": to,
        "subject": message.subject,
    }
    if cc:
        params["cc"] = cc
    if bcc:
        params["bcc"] = bcc

    html, text = _bodies(message)
    if html is not None:
        params["html"] = html
    if text is not None:
        params["text"] = text
    if message.reply_to:
        params["reply_to"] = _address(message.reply_to[0])

    headers: dict[str, str] = {}
    for name, value in message.extra_headers.items():
        lower = name.lower()
        if lower == IDEMPOTENCY_HEADER.lower():
            params["idempotency_key"] = str(value)
        elif _forwardable(lower):
            headers[name] = str(value)
    if headers:
        params["headers"] = headers

    tags = getattr(message, "tags", None)
    if tags:
        params["tags"] = [str(tag) for tag in tags]
    metadata = getattr(message, "metadata", None)
    if metadata:
        params["metadata"] = {str(key): str(value) for key, value in metadata.items()}

    attachments = [_attachment(item) for item in message.attachments]
    if attachments:
        params["attachments"] = attachments
    return params


def _bodies(message: EmailMessage) -> tuple[str | None, str | None]:
    """`(html, text)`. The body is HTML when `content_subtype == "html"`; a `text/html`
    alternative (`EmailMultiAlternatives`) is the HTML otherwise."""
    html: str | None = None
    text: str | None = None
    if message.content_subtype == "html":
        html = str(message.body) or None
    else:
        text = str(message.body) or None
    if isinstance(message, EmailMultiAlternatives):
        for content, mimetype in message.alternatives:
            if mimetype == "text/html" and html is None and isinstance(content, str):
                html = content
    return html, text


def _forwardable(lower_name: str) -> bool:
    if lower_name in _ALLOWED_HEADERS:
        return True
    return lower_name.startswith("x-") and not lower_name.startswith(_RESERVED_PREFIXES)


def _address(value: str) -> str:
    """`Name <address>`, or the bare address when Duva would refuse the name."""
    name, address = parseaddr(value)
    if not address:
        return value.strip()
    if not name or _UNSAFE_NAME.search(name):
        return address
    return duva.format_address(address, name)


def _addresses(values: Sequence[str]) -> list[str]:
    return [_address(value) for value in values]


def _attachment(item: Any) -> duva.Attachment:
    if isinstance(item, Message):
        content = item.get_payload(decode=True)
        if not isinstance(content, bytes):
            raise ValueError("Duva: this attachment has no readable content")
        content_id = item.get("Content-ID")
        disposition = str(item.get("Content-Disposition", "")).lower()
        return duva.Attachment.from_bytes(
            item.get_filename() or "attachment",
            content,
            content_type=item.get_content_type(),
            content_id=str(content_id).strip("<>")
            if content_id and "inline" in disposition
            else None,
        )
    filename, content, mimetype = item
    data = content.encode() if isinstance(content, str) else bytes(content)
    return duva.Attachment.from_bytes(filename or "attachment", data, content_type=mimetype)
