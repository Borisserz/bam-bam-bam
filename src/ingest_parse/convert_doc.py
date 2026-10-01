""".doc → .docx через LibreOffice."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class LibreOfficeNotFoundError(RuntimeError):
    pass


def convert_doc_to_docx(src: Path, *, output_dir: str | Path) -> Path:
    """Возвращает путь к .docx внутри output_dir."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice is None:
        raise LibreOfficeNotFoundError(
            "LibreOffice (soffice) is required to convert legacy .doc files. "
            "Install it, then retry. Examples:\n"
            "  Debian/Ubuntu: sudo apt install libreoffice-writer\n"
            "  Fedora:        sudo dnf install libreoffice-writer\n"
            "  macOS:         brew install --cask libreoffice\n"
            "Or convert the file to .docx manually and pass that path."
        )

    out_dir = Path(output_dir)
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
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"LibreOffice conversion timed out for {src}") from exc

    expected = out_dir / f"{src.stem}.docx"
    if proc.returncode != 0 or not expected.is_file():
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"Failed to convert {src} to docx via LibreOffice: {detail}")

    return expected
