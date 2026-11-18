import time
from collections.abc import Iterator

from google.genai import errors, types

from app.db import get_repo_files, record_usage, search_chunks
from app.embedder import embed_query, get_client, is_daily_quota_error

# Text-generation model used to write the answer.
# If you get a 404/403 error, check the current model names at
# https://ai.google.dev/gemini-api/docs/models and change this one line.
CHAT_MODEL = "gemini-3.5-flash-lite"

# Chunks below this cosine similarity are treated as unrelated. If the best chunk is
# below it, we skip the LLM call entirely. Tune this value if answers get refused too often.
MIN_SIMILARITY = 0.55

# How many earlier messages are sent along, and how much of each assistant reply
HISTORY_MESSAGES = 6
ASSISTANT_HISTORY_CHARS = 700

# Seconds to wait before each retry on rate limit (429) or temporary errors
RETRY_WAITS = [3, 10, 20]

NO_MATCH_MESSAGE = (
    "I could not find anything related to this question in the indexed code. "
    "Try asking about a specific file, function or feature."
)

SYSTEM_PROMPT = """You are CodeLens, an assistant that answers questions about a GitHub repository.

You receive a folder tree of the indexed files, numbered code snippets, and sometimes the earlier conversation.

Rules:
- Answer ONLY from the code snippets, the folder tree and the earlier conversation.
- If they do not contain the answer, say: "I could not find this in the indexed code."
- Cite the snippets you used with their numbers, like [1] or [2][3]. Do not cite the folder tree.
- Mention file names and function/class names when you know them.
- Use Markdown when it helps: short lists, tables, and fenced code blocks with a language tag.
- Keep the answer clear and concise.
- For follow-up questions, use the earlier conversation to understand what "it" or "that" refers to.
"""


def build_tree(paths: list[str], max_lines: int = 150) -> str:
    """Builds an indented folder tree from file paths. Falls back to folders only if it is long."""
    if not paths:
        return ""

    tree: dict = {}
    for path in paths:
        node = tree
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        node[parts[-1]] = None  # None marks a file

    def render(node: dict, depth: int, lines: list[str], with_files: bool) -> None:
        # Folders first, then files, both sorted by name
        for name in sorted(node, key=lambda n: (node[n] is None, n.lower())):
            child = node[name]
            if child is None:
                if with_files:
                    lines.append("  " * depth + name)
            else:
                lines.append("  " * depth + name)
                render(child, depth + 1, lines, with_files)

    lines: list[str] = []
    render(tree, 0, lines, True)
    if len(lines) > max_lines:
        lines = []
        render(tree, 0, lines, False)
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["..."]
    return "\n".join(lines)


def _retrieval_query(question: str, history: list[dict]) -> str:
    """Short follow-ups like "explain more" need the previous question to find the right code."""
    if history and len(question.split()) < 8:
        last_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        if last_user:
            return f"{last_user}\n{question}"
    return question


def _history_text(history: list[dict]) -> str:
    lines = []
    for m in history[-HISTORY_MESSAGES:]:
        if m["role"] == "user":
            lines.append(f'User: {m["content"]}')
        else:
            lines.append(f'Assistant: {m["content"][:ASSISTANT_HISTORY_CHARS]}')
    return "\n".join(lines)


def _build_context(chunks: list[dict]) -> str:
    """Formats the retrieved chunks as numbered snippets for the prompt."""
    parts = []
    for i, c in enumerate(chunks, start=1):
        parts.append(
            f'[{i}] {c["file_path"]} (lines {c["start_line"]}-{c["end_line"]})\n'
            f'```\n{c["content"]}\n```'
        )
    return "\n\n".join(parts)


def prepare_answer(repo_id: str, question: str, history: list[dict], top_k: int = 5) -> dict | None:
    """Retrieval step. Returns None if the repo has no chunks.

    Returns {"sources": [...], "prompt": str | None}. The prompt is None when nothing
    related was found, so the caller can skip the LLM call.
    """
    query_embedding = embed_query(_retrieval_query(question, history))
    found = search_chunks(repo_id, query_embedding, top_k)
    if not found:
        return None

    relevant = [c for c in found if c["similarity"] >= MIN_SIMILARITY]
    if not relevant:
        return {"sources": [], "prompt": None}

    parts = []
    tree = build_tree(get_repo_files(repo_id))
    if tree:
        parts.append("Indexed files of the repository (folder tree):\n" + tree)
    if history:
        parts.append("Conversation so far:\n" + _history_text(history))
    parts.append("Code snippets:\n\n" + _build_context(relevant))
    parts.append("Question: " + question)

    sources = [
        {
            "ref": i,
            "file_path": c["file_path"],
            "lines": f'{c["start_line"]}-{c["end_line"]}',
            "similarity": c["similarity"],
            "preview": c["content"][:300],
        }
        for i, c in enumerate(relevant, start=1)
    ]
    return {"sources": sources, "prompt": "\n\n".join(parts)}


def generate_stream(prompt: str) -> Iterator[str]:
    """Streams the answer text piece by piece. Retries only before the first piece arrives."""
    record_usage("chat_requests")
    for attempt in range(len(RETRY_WAITS) + 1):
        produced = False
        try:
            stream = get_client().models.generate_content_stream(
                model=CHAT_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.2,  # low = more factual, less creative
                ),
            )
            for chunk in stream:
                if chunk.text:
                    produced = True
                    yield chunk.text
            if not produced:
                yield "The model returned an empty answer."
            return
        except errors.APIError as e:
            if is_daily_quota_error(e):
                record_usage("daily_limit_hit")
            retryable = e.code in (429, 500, 503) and not is_daily_quota_error(e)
            if produced or not retryable or attempt == len(RETRY_WAITS):
                raise
            time.sleep(RETRY_WAITS[attempt])