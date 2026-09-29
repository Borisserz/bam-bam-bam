"""Конвертация legacy .doc → .docx через LibreOffice (soffice)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class LibreOfficeNotFoundError(RuntimeError):
    """LibreOffice/soffice отсутствует — без него .doc не конвертируем."""


def find_soffice() -> str | None:
    """Путь к soffice/libreoffice или None."""
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    return None


def convert_doc_to_docx(path: str | Path, *, output_dir: str | Path | None = None) -> Path:
    """
    Конвертировать .doc в .docx через LibreOffice headless.

    Возвращает путь к созданному .docx. Caller отвечает за cleanup, если
    output_dir — временный каталог.
    """
    src = Path(path).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"File not found: {src}")
    if src.suffix.lower() != ".doc":
        raise ValueError(f"Expected .doc, got: {src.suffix}")

    soffice = find_soffice()
    if soffice is None:
        raise LibreOfficeNotFoundError(
            "LibreOffice (soffice) is required to convert legacy .doc files. "
            "Install it, then retry. Examples:\n"
            "  Debian/Ubuntu: sudo apt install libreoffice-writer\n"
            "  Fedora:        sudo dnf install libreoffice-writer\n"
            "  macOS:         brew install --cask libreoffice\n"
            "Or convert the file to .docx manually and pass that path."
        )

    out_dir = (
        Path(output_dir)
        if output_dir is not None
        else Path(tempfile.mkdtemp(prefix="ingest-parse-doc-"))
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    # --headless: без UI; filter пишет OOXML Word
    cmd = [
        soffice,
        "--headless",
        "--nologo",
        "--nolockcheck",
        "--nodefault",
        "--nofirststartwizard",
        "--convert-to",
        "docx",
        "--outdir",
        str(out_dir),
        str(src),
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"LibreOffice conversion timed out for {src}") from exc

    expected = out_dir / f"{src.stem}.docx"
    if proc.returncode != 0 or not expected.is_file():
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit code {proc.returncode}"
        raise RuntimeError(f"Failed to convert {src} to docx via LibreOffice: {detail}")

    return expected
