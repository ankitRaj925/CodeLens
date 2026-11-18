import re

# Short, friendly messages like "hi" or "thanks" are answered locally,
# so they never use up the Gemini quota.

_GREETING = re.compile(
    r"^\s*(hi+|hello+|hey+|hola|yo|sup|namaste|namaskar|"
    r"(hi|hello|hey)\s+(there|codelens)|good\s+(morning|afternoon|evening|night))\W*$",
    re.IGNORECASE,
)
_HOW_ARE_YOU = re.compile(
    r"^\s*(how\s+are\s+you(\s+doing)?|how'?s\s+it\s+going|what'?s\s+up|kaise\s+ho)\W*$",
    re.IGNORECASE,
)
_THANKS = re.compile(
    r"^\s*(thanks|thank\s+you|thx|ty|thanks\s+a\s+lot|thank\s+you\s+so\s+much|shukriya|dhanyavad)\W*$",
    re.IGNORECASE,
)
_BYE = re.compile(r"^\s*(bye|goodbye|see\s+you|good\s+bye)\W*$", re.IGNORECASE)
_IDENTITY = re.compile(
    r"^\s*(who\s+are\s+you|what\s+are\s+you|what\s+is\s+codelens|what\s+can\s+you\s+do|help|how\s+do\s+you\s+work)\W*$",
    re.IGNORECASE,
)


def smalltalk_reply(question: str) -> str | None:
    """Returns a ready-made reply for greetings and similar messages, or None for real questions."""
    if _GREETING.match(question):
        return (
            "Hello! I'm CodeLens. Select a repository and ask me anything about it, "
            "for example where a feature is implemented, how to run it, or what a file does."
        )
    if _HOW_ARE_YOU.match(question):
        return "I'm doing well, thank you! What would you like to know about the repository?"
    if _THANKS.match(question):
        return "You're welcome! Let me know if you want to explore anything else in this repository."
    if _BYE.match(question):
        return "Goodbye! Come back any time you want to explore a repository."
    if _IDENTITY.match(question):
        return (
            "I'm CodeLens, an assistant that answers questions about public GitHub repositories. "
            "I search the indexed code and answer with sources (file names and line numbers), "
            "so you can verify every answer. Pick a repository and ask away."
        )
    return None