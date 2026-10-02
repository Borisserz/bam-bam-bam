""".doc/.rtf → .docx и .docx → .pdf через LibreOffice; .docm → .docx без макросов."""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

_DOCM_MAIN = b"application/vnd.ms-word.document.macroEnabled.main+xml"
_DOCX_MAIN = b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"


def docm_to_docx(src: Path, *, output_dir: str | Path) -> Path:
    """Тот же OOXML; python-docx (внутри Docling) не открывает тип содержимого macroEnabled."""
    out = Path(output_dir) / f"{src.stem}.docx"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info)
            if info.filename == "[Content_Types].xml":
                data = data.replace(_DOCM_MAIN, _DOCX_MAIN)
            zout.writestr(info, data)
    return out

# brew install --cask libreoffice не кладёт soffice в PATH
_SOFFICE_PATHS = (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/opt/homebrew/bin/soffice",
    "/usr/local/bin/soffice",
    "/usr/bin/soffice",
    "/usr/lib/libreoffice/program/soffice",
)


class LibreOfficeNotFoundError(RuntimeError):
    pass


def _windows_paths() -> list[str]:
    """Установщик Windows не добавляет soffice.exe в PATH."""
    roots = []
    for var in ("PROGRAMFILES", "PROGRAMW6432", "PROGRAMFILES(X86)"):
        root = os.environ.get(var)
        if root and root not in roots:
            roots.append(root)
    for default in (r"C:\Program Files", r"C:\Program Files (x86)"):
        if os.name == "nt" and default not in roots:
            roots.append(default)
    return [str(Path(r) / "LibreOffice" / "program" / "soffice.exe") for r in roots]


def _runnable(path: str) -> bool:
    return Path(path).is_file() and (path.lower().endswith(".exe") or os.access(path, os.X_OK))


def find_soffice() -> str | None:
    """LIBREOFFICE_PATH / SOFFICE → PATH → Program Files (Windows) → macOS/Linux."""
    for var in ("LIBREOFFICE_PATH", "SOFFICE"):
        override = os.environ.get(var)
        if override and _runnable(override):
            return override
    found = shutil.which("soffice.exe") or shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    return next((p for p in (*_windows_paths(), *_SOFFICE_PATHS) if _runnable(p)), None)


def convert_doc_to_docx(src: Path, *, output_dir: str | Path) -> Path:
    """Возвращает путь к .docx внутри output_dir."""
    return _soffice_convert(src, "docx", output_dir)


def docx_to_pdf(src: Path, *, output_dir: str | Path) -> Path:
    """Возвращает путь к .pdf внутри output_dir (рендер страниц со схемами)."""
    return _soffice_convert(src, "pdf", output_dir)


def _soffice_convert(src: Path, target: str, output_dir: str | Path) -> Path:
    soffice = find_soffice()
    if soffice is None:
        raise LibreOfficeNotFoundError(
            "LibreOffice (soffice) is required to convert .doc / .rtf files. "
            "Install it, then retry:\n"
            "  Windows:       winget install TheDocumentFoundation.LibreOffice\n"
            "                 (or https://www.libreoffice.org/download/; custom path: set LIBREOFFICE_PATH)\n"
            "  Debian/Ubuntu: sudo apt install libreoffice-writer\n"
            "  macOS:         brew install --cask libreoffice\n"
            "Or convert the file to .docx manually and pass that path."
        )

    out_dir = Path(output_dir)
    # свой профиль: иначе при открытом LibreOffice конвертация молча ничего не делает
    profile = (out_dir / ".lo-profile").resolve()
    cmd = [
        soffice,
        f"-env:UserInstallation={profile.as_uri()}",
        "--headless",
        "--nologo",
        "--nolockcheck",
        "--nodefault",
        "--nofirststartwizard",
        "--convert-to",
        target,
        "--outdir",
        str(out_dir),
        str(src),
    ]
    try:
        proc = subprocess.run(
            cmd, check=False, capture_output=True, text=True, errors="replace", timeout=120
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"LibreOffice conversion timed out for {src}") from exc

    expected = out_dir / f"{src.stem}.{target}"
    if proc.returncode != 0 or not expected.is_file():
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"Failed to convert {src} to {target} via LibreOffice: {detail}")

    return expected
