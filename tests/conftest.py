import json
import shutil
import sys
from datetime import date
from pathlib import Path

import pytest

BASIS = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(BASIS))
sys.path.insert(0, str(TESTS))

DATA = TESTS / "data"
GOLDEN = TESTS / "golden"
DATUM = date(2026, 11, 16)
XLSX = DATA / "misto_balans_2025.xlsx"
PDF = DATA / "misto_balans_2025.pdf"
JR2024_PDF = DATA / "misto_jaarrekening_2024.pdf"


@pytest.fixture
def klanten_root(tmp_path):
    root = tmp_path / "klanten"
    shutil.copytree(BASIS / "klanten" / "misto_interiors", root / "misto_interiors",
                    ignore=shutil.ignore_patterns("output", "ai_gebruik.jsonl"))
    return root


@pytest.fixture
def klant(klanten_root):
    from jaarboek.dossier import Klant
    return Klant(klanten_root / "misto_interiors")


@pytest.fixture
def kantoor():
    from jaarboek.dossier import laad_kantoor
    return laad_kantoor(BASIS / "kantoor.json")


@pytest.fixture(scope="session")
def golden_posten():
    return json.loads((GOLDEN / "misto_2025_posten.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def golden_tekst():
    return json.loads((GOLDEN / "misto_2025_pdf_tekst.json").read_text(encoding="utf-8"))


def pdf_tekst(pad):
    import pdfplumber
    with pdfplumber.open(pad) as pdf:
        return [p.extract_text() or "" for p in pdf.pages]
