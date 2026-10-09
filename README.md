# duva-django

Django email backend for [Duva](https://duva.ca), the transactional email API hosted in Canada. Use `send_mail()`,
`EmailMessage` and `EmailMultiAlternatives` as usual; the messages go out through Duva.

Requires Python 3.10+ and Django 5.2 or later (including the `MAILERS` setting of Django 6.1). Built on
[`duva-mail`](https://pypi.org/project/duva-mail/).

```bash
pip install duva-django
# or: uv add duva-django
```

You also need a Duva API key and a verified domain ([documentation](https://duva.ca/en/docs)).

## Configure

Django 5.2 and later, with `EMAIL_BACKEND`:

```python
# settings.py
EMAIL_BACKEND = "duva_django.EmailBackend"
DUVA_API_KEY = os.environ["DUVA_API_KEY"]
DUVA_DOMAIN = "example.com"
DEFAULT_FROM_EMAIL = "Example <notifications@example.com>"
```

Django 6.1 and later, with `MAILERS` (`EMAIL_BACKEND` is deprecated there):

```python
MAILERS = {
    "default": {
        "BACKEND": "duva_django.EmailBackend",
        "OPTIONS": {"api_key": os.environ["DUVA_API_KEY"], "domain": "example.com"},
    },
}
```

Options (`OPTIONS`, or the setting of the same name in capitals prefixed by `DUVA_`): `api_key`, `domain`, `base_url`
(default `https://api.duva.ca`), `timeout` (seconds, default 10), `max_retries` (default 2). `DUVA_API_KEY` and
`DUVA_DOMAIN` can also come from the environment. The `From` address must belong to the configured domain.

## What is sent

| Django | Duva |
|---|---|
| `from_email`, `to`, `cc`, `bcc`, `reply_to` | `from`, `to`, `cc`, `bcc`, `reply_to` |
| Plain body, `content_subtype = "html"`, `attach_alternative(html, "text/html")` | `text`, `html` |
| `attach()` files and inline images (`Content-ID`) | `attachments` (inline ones keep their `content_id`) |
| `message.tags = [...]` | `tags` |
| `message.metadata = {...}` | `metadata` |
| `List-Unsubscribe`, `List-Unsubscribe-Post`, `List-Id`, `In-Reply-To`, `References`, `Auto-Submitted`, `Precedence`, `Importance`, `Feedback-ID`, and `X-*` headers in `headers` | `headers` |

(`tags` and `metadata` follow the django-anymail convention, so code written for it keeps working.)

Notes:

- Other headers are not forwarded (Duva rejects them), nor are `X-Duva*`, `X-Kumo*`, `X-Tenant*` and `X-Campaign*`.
- A display name Duva refuses (it contains `@`, quotes, `<>`, control characters or an encoded word `=?…?=`) is dropped;
  the address is kept.
- Delivery is asynchronous: a successful send means Duva accepted the message, not that it was delivered. After a send,
  `message.duva_result` holds the `duva.SendMessageResult` (`id`, `status`, `replayed`). Use Duva's webhooks or events
  for the outcome.
- A Duva error (`duva.DuvaError` and its subclasses) is raised, unless `fail_silently=True`, in which case the message is
  not counted in the return value of `send_mail()`. A missing key or domain raises `ImproperlyConfigured`.

### Idempotency

A retried task builds a new message, so it would be sent twice. Set a stable key for it (an order ID, for example):

```python
EmailMessage(
    subject,
    body,
    from_email,
    [to],
    headers={"X-Idempotency-Key": f"order-{order.id}"},
).send()
```

The header is read by the backend and never sent. Duva ignores a second message with the same key.

## Testing your application

Django's test runner swaps the backend for the in-memory one, so `mail.outbox` works as usual. To exercise this backend
without a network, give it a client built on a fake HTTP transport:

```python
import duva, httpx
from duva_django import EmailBackend

client = duva.Duva(
    api_key="dv_test",
    domain="example.com",
    http_client=httpx.Client(transport=httpx.MockTransport(handler)),
)
EmailBackend(client=client).send_messages([message])
```

## License

MIT.
