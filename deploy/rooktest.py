"""Rooktest in de container: Chromium maakt een PDF met de juiste letters, en de app-modules laden."""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pdfplumber  # noqa: E402

import jaarboek.doorrol  # noqa: E402,F401  (laadt alle modules die de app gebruikt)
from jaarboek.render import chrome_pad, chrome_vlaggen  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    html = Path(tmp) / "t.html"
    html.write_text('<html><body style="font-family: Helvetica, Arial, sans-serif; font-size: 10pt">'
                    "Jaarrapport 2025 · Balans per 31 december 2025 · € 12.345</body></html>", encoding="utf-8")
    pdf = Path(tmp) / "t.pdf"
    r = subprocess.run([chrome_pad(), *chrome_vlaggen(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        f"--user-data-dir={Path(tmp) / 'profiel'}", f"--print-to-pdf={pdf}", html.as_uri()],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and pdf.exists(), r.stderr[-800:]
    with pdfplumber.open(pdf) as d:
        tekst = d.pages[0].extract_text()
        fonts = {c["fontname"] for c in d.pages[0].chars}
    assert "Balans per 31 december 2025" in tekst and "€ 12.345" in tekst, tekst
    assert any("Liberation" in f or "Helvetica" in f or "Arial" in f for f in fonts), fonts
    print("rooktest OK:", sorted(fonts))
