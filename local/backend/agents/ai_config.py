"""Shared provider settings for backend review and recovery agents."""

import os
import math
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

TAMU_BASE_URL = "https://chat-api.tamu.ai/openai"
TAMU_DEFAULT_MODEL = "protected.gpt-5.4-nano"
RATE_LIMIT_DELAYS = (15, 30, 60, 120)


def retry_after_seconds(error):
    """Read Retry-After as seconds or an HTTP date; invalid values use backoff."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    if raw is None:
        return None
    try:
        delay = float(raw)
    except (ValueError, TypeError):
        try:
            target = parsedate_to_datetime(str(raw))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            delay = max(0, (target - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def request_with_retry(operation, **kwargs):
    """Retry the same API request on HTTP 429, with at most four resends."""
    for attempt in range(len(RATE_LIMIT_DELAYS) + 1):
        try:
            return operation(**kwargs)
        except Exception as error:
            response = getattr(error, "response", None)
            status = getattr(error, "status_code", None)
            if status is None:
                status = getattr(response, "status_code", None)
            if status != 429:
                raise
            if attempt == len(RATE_LIMIT_DELAYS):
                print("Backend AI: HTTP 429 retry limit reached after 5 requests.", flush=True)
                raise
            delay = retry_after_seconds(error)
            source = "Retry-After"
            if delay is None:
                delay = RATE_LIMIT_DELAYS[attempt] + random.uniform(0, 1)
                source = "backoff"
            print(f"Backend AI: HTTP 429; waiting {delay:.2f}s ({source}) before retry "
                  f"{attempt + 1}/{len(RATE_LIMIT_DELAYS)}.", flush=True)
            time.sleep(delay)


def using_tamu():
    return bool(os.getenv("TAMUS_AI_CHAT_API_KEY"))


def api_key():
    return os.getenv("TAMUS_AI_CHAT_API_KEY") or os.getenv("OPENAI_API_KEY")


def base_url():
    if using_tamu():
        return os.getenv("TAMU_CHAT_BASE_URL") or os.getenv("TAMU_BASE_URL") or TAMU_BASE_URL
    return os.getenv("OPENAI_BASE_URL")


def model_name(default):
    if using_tamu():
        return os.getenv("TAMU_MODEL") or TAMU_DEFAULT_MODEL
    return os.getenv("OPENAI_MODEL", default)


class ResponsesUnavailable(RuntimeError):
    """The selected provider uses Chat Completions only."""


def create_response(client, **kwargs):
    if using_tamu():
        raise ResponsesUnavailable("TAMU uses the Chat Completions endpoint")
    return request_with_retry(client.responses.create, **kwargs)


def create_chat_completion(client, **kwargs):
    return request_with_retry(client.chat.completions.create, **kwargs)
