"""Scan tracked/source files for anything that looks like a live credential.

    python -m scripts.secret_scan

Used by preflight and meant to be run before every commit and before recording the demo.
"""

from __future__ import annotations

import os
import re
import sys

PATTERNS = [
    ("openai/groq style key", re.compile(r"\b(sk|gsk)_[A-Za-z0-9_-]{20,}\b")),
    ("bearer token", re.compile(r"Bearer\s+[A-Za-z0-9._-]{24,}", re.I)),
    ("deepgram-like hex key", re.compile(r"\b[0-9a-f]{40}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("env assignment with real-looking value", re.compile(r"^(RIME|DEEPGRAM|OPENAI|LLM|GROQ)[A-Z_]*KEY\s*=\s*['\"]?(?!your_|\$\{|<)[A-Za-z0-9._-]{16,}", re.M)),
]
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "evidence", "dist", "build"}
SKIP_FILES = {".env"}  # .env is git-ignored and legitimately holds the key locally
TEXT_EXT = {".py", ".js", ".ts", ".md", ".json", ".toml", ".yaml", ".yml", ".txt", ".html", ".css", ".example", ".sh", ".cfg", ".ini"}


def scan_repo(root: str) -> list[str]:
    findings: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_FILES or fn.startswith(".env."):
                continue
            ext = os.path.splitext(fn)[1]
            if ext not in TEXT_EXT and fn not in (".env.example", ".gitignore", "Makefile"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
            except OSError:
                continue
            for label, rx in PATTERNS:
                for m in rx.finditer(text):
                    line = text.count("\n", 0, m.start()) + 1
                    findings.append(f"{os.path.relpath(path, root)}:{line}: {label}")
    return findings


def main() -> None:
    findings = scan_repo(os.getcwd())
    for f in findings:
        print(f)
    print(f"{len(findings)} finding(s)")
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
