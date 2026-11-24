"""
plugins/_doc_text.py — plain text from the file the user dropped on the HUD.

Helper module (leading underscore: the plugin loader skips it). Used by quiz
and spaced_repetition for "quiz me on this" / "make flashcards from this".

Every optional reader is imported at use and failure is reported as an empty
string plus a reason — never an exception.
"""
from __future__ import annotations

from pathlib import Path

_TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".log",
             ".html", ".htm", ".xml", ".py", ".js", ".ts", ".java", ".c", ".cpp",
             ".h", ".cs", ".go", ".rs", ".rb", ".php", ".tex", ".yaml", ".yml", ".ini"}


def uploaded_file(player) -> str:
    """Path of the file currently in the HUD's upload box, or ''."""
    if player is None:
        return ""
    try:
        path = getattr(player, "current_file", None)
        path = path() if callable(path) else path
        return str(path or "")
    except Exception:
        return ""


def read_text(path: str, max_chars: int = 40_000) -> tuple[str, str]:
    """(text, error). error is '' on success."""
    try:
        p = Path(path).expanduser()
        if not p.is_file():
            return "", "the uploaded item is not a readable file"
        ext = p.suffix.lower()
        text = ""
        if ext == ".pdf":
            try:
                import pdfplumber
                with pdfplumber.open(p) as pdf:
                    parts = []
                    for page in pdf.pages:
                        parts.append(page.extract_text() or "")
                        if sum(len(x) for x in parts) > max_chars:
                            break
                    text = "\n".join(parts)
            except ImportError:
                try:
                    import PyPDF2
                    reader = PyPDF2.PdfReader(str(p))
                    text = "\n".join((pg.extract_text() or "") for pg in reader.pages[:200])
                except ImportError:
                    return "", "no PDF reader is installed (pip install pdfplumber)"
            if not text.strip():
                return "", "that PDF has no extractable text (it may be scanned)"
        elif ext == ".docx":
            try:
                from docx import Document
            except ImportError:
                return "", "python-docx is not installed"
            text = "\n".join(par.text for par in Document(str(p)).paragraphs)
        elif ext == ".pptx":
            try:
                from pptx import Presentation
            except ImportError:
                return "", "python-pptx is not installed"
            lines = []
            for slide in Presentation(str(p)).slides:
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        lines.append(shape.text_frame.text)
            text = "\n".join(lines)
        elif ext in _TEXT_EXT or not ext:
            text = p.read_text(encoding="utf-8", errors="ignore")
        else:
            return "", f"I can't read {ext} files as text"
        text = text.strip()
        if not text:
            return "", "the file is empty"
        return text[:max_chars], ""
    except Exception as e:
        return "", f"reading the file failed: {e}"
