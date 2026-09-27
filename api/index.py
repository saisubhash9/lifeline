"""Vercel entry point: the whole app (API, dashboard, data) is one FastAPI ASGI function."""

from backend.app import app  # noqa: F401
