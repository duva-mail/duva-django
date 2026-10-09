"""Django email backend for Duva. Use `EMAIL_BACKEND = "duva_django.EmailBackend"`."""

from duva_django.backend import IDEMPOTENCY_HEADER, EmailBackend

__all__ = ["IDEMPOTENCY_HEADER", "EmailBackend"]
__version__ = "0.1.0"
