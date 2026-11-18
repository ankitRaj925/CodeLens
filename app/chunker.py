from pathlib import Path

from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

# Maps a file extension to the splitter language.
# Extensions missing here fall back to a generic text splitter.
EXTENSION_TO_LANGUAGE = {
    ".py": "python",
    ".js": "js",
    ".jsx": "js",
    ".ts": "ts",
    ".tsx": "ts",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".md": "markdown",
}

# Max characters per chunk, and how many characters neighbouring chunks share.
# The overlap keeps context when a split falls in an awkward place.
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 150


def _build_splitter(extension: str) -> RecursiveCharacterTextSplitter:
    """Returns a language-aware splitter, or a generic one as a fallback."""
    lang_name = EXTENSION_TO_LANGUAGE.get(extension)
    if lang_name:
        try:
            return RecursiveCharacterTextSplitter.from_language(
                language=Language(lang_name),
                chunk_size=CHUNK_SIZE,
                chunk_overlap=CHUNK_OVERLAP,
                add_start_index=True,  # records where each chunk starts in the file
            )
        except ValueError:
            pass  # this language is not supported by the installed version
    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        add_start_index=True,
    )


def chunk_file(repo_path: Path, relative_path: Path) -> list[dict]:
    """Splits one file into chunks and returns them with file path and line numbers."""
    full_path = repo_path / relative_path

    # errors="ignore" skips bytes that are not valid UTF-8 instead of crashing
    text = full_path.read_text(encoding="utf-8", errors="ignore")
    if not text.strip():
        return []

    splitter = _build_splitter(relative_path.suffix.lower())
    documents = splitter.create_documents([text])

    chunks: list[dict] = []
    for doc in documents:
        start_index = doc.metadata["start_index"]
        # Line number = how many newlines come before this position, plus 1
        start_line = text.count("\n", 0, start_index) + 1
        end_line = start_line + doc.page_content.count("\n")
        chunks.append(
            {
                "file_path": relative_path.as_posix(),
                "start_line": start_line,
                "end_line": end_line,
                "content": doc.page_content,
            }
        )
    return chunks


def chunk_repo(repo_path: Path, files: list[Path]) -> list[dict]:
    """Chunks every file in the list and returns one flat list of chunks."""
    all_chunks: list[dict] = []
    for relative_path in files:
        all_chunks.extend(chunk_file(repo_path, relative_path))
    return all_chunks