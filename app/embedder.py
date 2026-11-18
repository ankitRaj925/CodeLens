import math
import time
from typing import Callable

from google import genai
from google.genai import errors, types

from app.config import settings
from app.db import EMBEDDING_DIM, PACIFIC, record_usage

EMBEDDING_MODEL = "gemini-embedding-001"

# How many texts we send to Gemini in one request
BATCH_SIZE = 50

# Seconds to wait before each retry when the API is rate limited or busy
RETRY_WAITS = [5, 15, 30, 60]

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Creates the Gemini client once and reuses it."""
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.google_api_key)
    return _client


# Public alias so other modules can reuse the same Gemini client
get_client = _get_client


def is_daily_quota_error(e: errors.APIError) -> bool:
    """True when a 429 error comes from the per-day quota (retrying will not help)."""
    return e.code == 429 and "PerDay" in str(e)


def _time_until_reset() -> str:
    """Time left until the daily quota resets (midnight Pacific time), like '2h 15m'."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(PACIFIC)
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # Subtract in UTC so daylight saving changes cannot skew the result
    seconds = int(
        (next_midnight.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    )
    hours, minutes = divmod(max(seconds, 0) // 60, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def gemini_error_message(e: errors.APIError) -> str:
    """Turns a Gemini API error into a message a user can act on."""
    if is_daily_quota_error(e):
        return (
            "Daily quota reached. It resets at midnight Pacific time, "
            f"in about {_time_until_reset()}. Please try again after that, or use a smaller repo."
        )
    if e.code == 429:
        return "Rate limit reached. Please wait a few minutes and try again, or use a smaller repo."
    if e.code in (401, 403):
        return "Gemini rejected the request. Please check GOOGLE_API_KEY in your .env file."
    if e.code == 404:
        return "Gemini model not found. Please check the model names in embedder.py and rag.py."
    return f"Gemini API error ({e.code}). Please try again."


def _normalize(vec: list[float]) -> list[float]:
    """Scales a vector to length 1. Needed for reduced-size Gemini embeddings."""
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _embed_batch(texts: list[str], task_type: str) -> list[list[float]]:
    """Embeds one batch, retrying on rate limit (429) or temporary server errors."""
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            result = _get_client().models.embed_content(
                model=EMBEDDING_MODEL,
                contents=texts,
                config=types.EmbedContentConfig(
                    task_type=task_type,
                    output_dimensionality=EMBEDDING_DIM,
                ),
            )
            record_usage("embed_requests")
            record_usage("embedded_chunks", len(texts))
            return [_normalize(e.values) for e in result.embeddings]
        except errors.APIError as e:
            if is_daily_quota_error(e):
                record_usage("daily_limit_hit")
            retryable = e.code in (429, 500, 503) and not is_daily_quota_error(e)
            if not retryable or attempt == len(RETRY_WAITS):
                raise
            time.sleep(RETRY_WAITS[attempt])
    return []  # unreachable, keeps type checkers happy


def embed_texts(
    texts: list[str],
    task_type: str = "RETRIEVAL_DOCUMENT",
    on_progress: Callable[[int, int], None] | None = None,
) -> list[list[float]]:
    """Embeds any number of texts by splitting them into batches.

    on_progress(done, total) is called after every batch, so callers can show a progress bar.
    """
    all_embeddings: list[list[float]] = []
    total = len(texts)
    for i in range(0, total, BATCH_SIZE):
        all_embeddings.extend(_embed_batch(texts[i : i + BATCH_SIZE], task_type))
        if on_progress:
            on_progress(len(all_embeddings), total)
        time.sleep(1)  # small pause between batches to stay within rate limits
    return all_embeddings


def embed_query(question: str) -> list[float]:
    """Embeds a user question. Uses RETRIEVAL_QUERY, the counterpart of RETRIEVAL_DOCUMENT."""
    return _embed_batch([question], "RETRIEVAL_QUERY")[0]