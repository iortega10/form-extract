from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_BLOCK = """
import sys
class _BlockPymupdf:
    def find_spec(self, name, path=None, target=None):
        if name == "pymupdf" or name.startswith("pymupdf."):
            raise ImportError("pymupdf blocked for test")
        return None
sys.meta_path.insert(0, _BlockPymupdf())
"""


def _run_blocked(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _BLOCK + script],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_xlsx_path_does_not_require_pymupdf(tmp_path: Path):
    from openpyxl import Workbook

    xlsx = tmp_path / "plain.xlsx"
    wb = Workbook()
    wb.active["A1"] = "Label"
    wb.save(xlsx)

    script = f"""
from formextract.ingest import ingest
result = ingest({str(xlsx)!r})
assert result.backend.name == "xlsx"
print("ok")
"""
    proc = _run_blocked(script)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_pdf_path_raises_typed_error_without_extra(tmp_path: Path):
    pdf = tmp_path / "scanned.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    script = f"""
from formextract.ingest import ingest
from formextract.ingest.pdf_text import PdfBackendUnavailable
try:
    ingest({str(pdf)!r})
except PdfBackendUnavailable as exc:
    assert "form-extract[pdf]" in str(exc)
    print("ok")
else:
    raise SystemExit("expected PdfBackendUnavailable")
"""
    proc = _run_blocked(script)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
