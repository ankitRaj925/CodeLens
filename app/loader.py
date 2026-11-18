import os
import re
import shutil
from pathlib import Path

from git import Repo

# Cloned repositories are stored here inside the container
REPOS_DIR = Path("/tmp/repos")

# Only public GitHub URLs like https://github.com/owner/repo are accepted
GITHUB_URL_RE = re.compile(
    r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/?#].*)?$"
)

# File types we want to index
ALLOWED_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go",
    ".rs", ".cpp", ".c", ".h", ".cs", ".rb", ".php", ".md",
}

# Folders that contain generated or third-party code, so we skip them
SKIP_DIRS = {
    ".git", ".github", "node_modules", "venv", ".venv", "__pycache__",
    "dist", "build", ".idea", ".vscode", "target", "vendor",
    # Docs and tests add many chunks but rarely answer "how does the code work"
    "docs", "tests", "test", "__tests__",
}

# Files bigger than this are usually generated or data files
MAX_FILE_SIZE = 200 * 1024  # 200 KB


def parse_github_url(url: str) -> tuple[str, str]:
    """Returns (owner, repo_name) or raises ValueError for invalid URLs."""
    match = GITHUB_URL_RE.match(url.strip())
    if not match:
        raise ValueError(
            "Invalid URL. Use a public GitHub URL like https://github.com/owner/repo"
        )
    return match.group(1), match.group(2)


def normalize_folder(folder: str | None) -> str:
    """Cleans the optional folder input. Raises ValueError for unsafe or odd values."""
    if not folder:
        return ""
    cleaned = folder.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.strip("/")
    if cleaned in ("", "."):
        return ""
    parts = cleaned.split("/")
    # Block path tricks such as "..", drive letters and home paths
    if any(p in ("", "..") for p in parts) or ":" in cleaned or cleaned.startswith("~"):
        raise ValueError("Invalid folder. Use a relative path like src or packages/core.")
    return "/".join(parts)


def clone_repo(url: str) -> Path:
    """Shallow-clones the repository and returns the local folder path."""
    owner, name = parse_github_url(url)
    target = REPOS_DIR / f"{owner}__{name}"

    # Remove any old copy so we always index a fresh version
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    REPOS_DIR.mkdir(parents=True, exist_ok=True)

    # depth=1 downloads only the latest commit, which is much faster
    Repo.clone_from(f"https://github.com/{owner}/{name}.git", target, depth=1)
    return target


def list_code_files(repo_path: Path, folder: str = "") -> list[Path]:
    """Walks the repo (or one folder of it) and returns relative paths of files worth indexing."""
    base = repo_path / folder if folder else repo_path

    if folder:
        resolved = base.resolve()
        try:
            # The folder must stay inside the cloned repo (blocks symlink tricks)
            resolved.relative_to(repo_path.resolve())
        except ValueError:
            raise ValueError("Invalid folder. Use a relative path like src or packages/core.")
        if not resolved.is_dir():
            raise ValueError(f"Folder '{folder}' was not found in the repository.")

    files: list[Path] = []
    for root, dirs, filenames in os.walk(base):
        # Editing dirs in place stops os.walk from entering skipped folders
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in filenames:
            path = Path(root) / filename
            # Symlinks could point to files outside the repo, so we never follow them
            if path.is_symlink():
                continue
            if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue
            if path.stat().st_size > MAX_FILE_SIZE:
                continue
            files.append(path.relative_to(repo_path))
    return sorted(files)