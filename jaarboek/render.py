"""Jaarrekening renderen: Jinja2-HTML -> PDF via headless Chrome."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import TEMPLATES
from .structuur import fmt

_CHROME_KANDIDATEN = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


class RenderFout(Exception):
    pass


def chrome_vlaggen() -> list[str]:
    """Extra Chrome-opties in een container (Docker/Hugging Face): geen sandbox en geen kleine /dev/shm."""
    if os.environ.get("JAARBOEK_CONTAINER") or (hasattr(os, "geteuid") and os.geteuid() == 0):
        return ["--no-sandbox", "--disable-dev-shm-usage"]
    return []


def chrome_pad() -> str:
    if os.environ.get("JAARBOEK_CHROME"):
        return os.environ["JAARBOEK_CHROME"]
    for p in _CHROME_KANDIDATEN:
        if Path(p).exists():
            return p
    for naam in ("google-chrome", "chromium", "chromium-browser", "msedge"):
        if shutil.which(naam):
            return shutil.which(naam)
    raise RenderFout("Google Chrome (of Chromium/Edge) niet gevonden; nodig om de PDF te maken.")


def render_html(kolommen: list[dict], meta: dict, labels: dict) -> str:
    """kolommen: posten per jaarkolom, nieuwste eerst (2 of meer)."""
    env = Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined, autoescape=True)
    env.filters["n"] = fmt
    J = kolommen

    def rows(keys):
        return [(labels[k], [j.get(k, 0) for j in J]) for k in keys if any(j.get(k, 0) != 0 for j in J)]

    def toon(k):
        return any(j.get(k, 0) != 0 for j in J)

    def tot(k):
        return [j.get(k, 0) for j in J]

    jaren = meta.get("jaren") or [meta["jaar"] - i for i in range(len(J))]
    meta = {**meta, "n": len(J), "jaren": jaren}
    kop = lambda i: "Verslagjaar" if i == 0 else "Vorig jaar" if i == 1 else ""  # noqa: E731
    KB = [(kop(i), f"31-12-{j}") for i, j in enumerate(jaren)]   # balans: per balansdatum
    KW = [(kop(i), str(j)) for i, j in enumerate(jaren)]          # W&V: per boekjaar

    return env.get_template("jaarrekening.html.j2").render(J=J, c=J[0], p=J[1], m=meta, rows=rows, toon=toon,
                                                             tot=tot, L=labels, KB=KB, KW=KW)


def render_pdf(kolommen: list[dict], meta: dict, labels: dict, uit_pdf: Path, uit_html: Path | None = None) -> Path:
    html = render_html(kolommen, meta, labels)
    uit_pdf = Path(uit_pdf)
    uit_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        html_pad = Path(uit_html) if uit_html else Path(tmp) / "jaarrekening.html"
        html_pad.write_text(html, encoding="utf-8")
        if uit_pdf.exists():
            uit_pdf.unlink()
        cmd = [chrome_pad(), *chrome_vlaggen(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
               f"--user-data-dir={Path(tmp) / 'profiel'}", "--no-first-run", "--no-default-browser-check",
               f"--print-to-pdf={uit_pdf}", html_pad.resolve().as_uri()]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0 or not uit_pdf.exists():
            raise RenderFout(f"PDF maken mislukt: {r.stderr[-500:]}")
    return uit_pdf


def pdf_paginas(pdf_pad: Path, schaal: float = 1.3):
    """PDF-pagina's als PIL-afbeeldingen (voor de voorbeeldweergave in de app)."""
    import pypdfium2 as pdfium
    from . import PDFIUM_SLOT
    with PDFIUM_SLOT:
        doc = pdfium.PdfDocument(str(pdf_pad))
        try:
            return [doc[i].render(scale=schaal).to_pil() for i in range(len(doc))]
        finally:
            doc.close()


def pdf_pagina(pdf_pad: Path, nr: int, schaal: float = 1.3):
    """Eén PDF-pagina (0-based) als PIL-afbeelding."""
    import pypdfium2 as pdfium
    from . import PDFIUM_SLOT
    with PDFIUM_SLOT:
        doc = pdfium.PdfDocument(str(pdf_pad))
        try:
            pg = doc[nr]
            try:
                return pg.render(scale=schaal).to_pil()
            finally:
                pg.close()
        finally:
            doc.close()
