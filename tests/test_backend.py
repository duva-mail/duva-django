from __future__ import annotations

import base64
import json
from email.mime.image import MIMEImage
from typing import Any

import django
import duva
import httpx
import pytest
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage, EmailMultiAlternatives, get_connection
from django.test import override_settings

from duva_django import EmailBackend


class Recorder:
    """A fake Duva API: records every request, answers with `handler`."""

    def __init__(self, handler: Any = None) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler or (
            lambda request: httpx.Response(
                202, json={"id": "msg_" + "0" * 31 + "1", "status": "queued"}
            )
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    @property
    def body(self) -> dict[str, Any]:
        assert len(self.requests) == 1
        return json.loads(self.requests[0].content)

    def backend(self, **kwargs: Any) -> EmailBackend:
        client = duva.Duva(
            api_key="dv_test",
            domain="example.com",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(self)),
        )
        return EmailBackend(client=client, **kwargs)


@pytest.fixture
def api() -> Recorder:
    return Recorder()


def test_maps_a_message_to_the_duva_request(api: Recorder) -> None:
    message = EmailMultiAlternatives(
        "Order",
        "Plain text",
        "Shop <shop@example.com>",
        ["Client <a@example.org>"],
        cc=["cc@example.org"],
        bcc=["hidden@example.org"],
        reply_to=["Support <help@example.com>"],
    )
    message.attach_alternative("<p>Hi</p>", "text/html")

    assert api.backend().send_messages([message]) == 1

    request = api.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/example.com/messages"
    assert request.headers["authorization"] == "Bearer dv_test"
    assert api.body == {
        "from": "Shop <shop@example.com>",
        "to": ["Client <a@example.org>"],
        "cc": ["cc@example.org"],
        "bcc": ["hidden@example.org"],
        "subject": "Order",
        "text": "Plain text",
        "html": "<p>Hi</p>",
        "reply_to": "Support <help@example.com>",
    }


def test_an_html_body_is_sent_as_html(api: Recorder) -> None:
    message = EmailMessage("S", "<b>x</b>", "shop@example.com", ["a@example.org"])
    message.content_subtype = "html"

    api.backend().send_messages([message])

    assert api.body["html"] == "<b>x</b>"
    assert "text" not in api.body


def test_reports_duvas_result_on_the_message(api: Recorder) -> None:
    message = EmailMessage("S", "x", "shop@example.com", ["a@example.org"])

    api.backend().send_messages([message])

    assert message.duva_result.id == "msg_" + "0" * 31 + "1"  # type: ignore[attr-defined]
    assert message.duva_result.status == "queued"  # type: ignore[attr-defined]


def test_drops_a_display_name_duva_would_refuse_but_keeps_the_address(api: Recorder) -> None:
    message = EmailMessage(
        "S",
        "x",
        "shop@example.com",
        ['"Mallory @ evil.com" <a@example.org>', "Foo <b@example.org>"],
        cc=["=?UTF-8?B?Zm9v?= <c@example.org>"],
    )

    api.backend().send_messages([message])

    assert api.body["to"] == ["a@example.org", "Foo <b@example.org>"]
    assert api.body["cc"] == ["c@example.org"]


def test_forwards_tags_metadata_and_allowed_headers_only(api: Recorder) -> None:
    message = EmailMessage(
        "S",
        "x",
        "shop@example.com",
        ["a@example.org"],
        headers={
            "List-Unsubscribe": "<https://example.com/u/1>",
            "X-Custom": "yes",
            "X-Duva-Secret": "no",
            "Organization": "Acme",
            "Message-ID": "<x@y>",
        },
    )
    message.tags = ["welcome"]  # type: ignore[attr-defined]
    message.metadata = {"order_id": 42}  # type: ignore[attr-defined]

    api.backend().send_messages([message])

    assert api.body["tags"] == ["welcome"]
    assert api.body["metadata"] == {"order_id": "42"}
    assert api.body["headers"] == {
        "List-Unsubscribe": "<https://example.com/u/1>",
        "X-Custom": "yes",
    }


def test_the_idempotency_header_becomes_the_key_and_is_not_forwarded(api: Recorder) -> None:
    message = EmailMessage(
        "S", "x", "shop@example.com", ["a@example.org"], headers={"X-Idempotency-Key": "order-42"}
    )

    api.backend().send_messages([message])

    assert api.requests[0].headers["idempotency-key"] == "order-42"
    assert "headers" not in api.body


def test_attachments_and_inline_images(api: Recorder) -> None:
    message = EmailMessage("S", "x", "shop@example.com", ["a@example.org"])
    message.attach("note.txt", "hello", "text/plain")
    image = MIMEImage(b"\x89PNG\r\n\x1a\n" + b"0" * 16, "png")
    image.add_header("Content-ID", "<logo>")
    image.add_header("Content-Disposition", "inline", filename="logo.png")
    message.attach(image)

    api.backend().send_messages([message])

    by_name = {a["filename"]: a for a in api.body["attachments"]}
    assert base64.b64decode(by_name["note.txt"]["content"]) == b"hello"
    assert by_name["note.txt"]["content_type"] == "text/plain"
    assert by_name["note.txt"]["content_id"] is None
    assert by_name["logo.png"]["content_id"] == "logo"
    assert by_name["logo.png"]["content_type"] == "image/png"


def test_a_duva_error_is_raised_unless_fail_silently() -> None:
    rejected = Recorder(
        lambda request: httpx.Response(
            422, json={"error": {"code": "invalid_request", "message": "Invalid message"}}
        )
    )
    message = EmailMessage("S", "x", "shop@example.com", ["a@example.org"])

    with pytest.raises(duva.ValidationError):
        rejected.backend().send_messages([message])
    assert rejected.backend(fail_silently=True).send_messages([message]) == 0


def test_counts_only_the_messages_that_were_accepted() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(422, json={"error": {"code": "invalid_request", "message": "x"}})
        return httpx.Response(202, json={"id": "msg_" + "0" * 32, "status": "queued"})

    api = Recorder(handler)
    messages = [EmailMessage("S", "x", "shop@example.com", [f"{i}@example.org"]) for i in range(2)]

    assert api.backend(fail_silently=True).send_messages(messages) == 1


def test_a_message_without_recipient_is_refused() -> None:
    api = Recorder()

    with pytest.raises(ValueError, match="no recipient"):
        api.backend().send_messages([EmailMessage("S", "x", "shop@example.com", [])])
    assert api.requests == []


def test_django_send_mail_uses_the_configured_backend() -> None:
    api = Recorder()
    connection = get_connection("duva_django.EmailBackend", client=api.backend()._client)

    sent = mail.send_mail(
        "Hi", "Body", "shop@example.com", ["a@example.org"], connection=connection
    )

    assert sent == 1
    assert api.body["subject"] == "Hi"


@override_settings(DUVA_API_KEY=None, DUVA_DOMAIN=None)
def test_missing_configuration_is_an_error_not_a_silent_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DUVA_API_KEY", raising=False)
    monkeypatch.delenv("DUVA_DOMAIN", raising=False)
    message = EmailMessage("S", "x", "shop@example.com", ["a@example.org"])

    with pytest.raises(ImproperlyConfigured, match="API key"):
        EmailBackend().send_messages([message])
    assert EmailBackend(fail_silently=True).send_messages([message]) == 0


def test_options_win_over_settings_and_reach_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeDuva:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.messages = self

        def send(self, **params: Any) -> Any:
            return duva.SendMessageResult(id="m", status="queued", replayed=False, location=None)

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(duva, "Duva", FakeDuva)
    backend = EmailBackend(api_key="dv_opt", domain="opt.example", timeout=3, max_retries=5)

    assert backend.send_messages([EmailMessage("S", "x", "a@opt.example", ["b@example.org"])]) == 1
    assert captured["api_key"] == "dv_opt" and captured["domain"] == "opt.example"
    assert captured["timeout"] == 3.0 and captured["max_retries"] == 5
    assert captured["base_url"] == "https://api.duva.ca"
    assert captured["closed"] is True


@pytest.mark.skipif(django.VERSION < (6, 1), reason="MAILERS arrives in Django 6.1")
def test_works_as_a_mailers_entry() -> None:
    from django.core.mail import mailers

    with override_settings(
        MAILERS={
            "default": {
                "BACKEND": "duva_django.EmailBackend",
                "OPTIONS": {"api_key": "dv_x", "domain": "example.com"},
            }
        }
    ):
        backend = mailers["default"]

        assert isinstance(backend, EmailBackend)
        assert backend.alias == "default"
