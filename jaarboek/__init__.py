"""Jaarboek: concept-jaarrekening uit een Asperion-balans (PDF of Excel).

Alle bedragen komen uit code en worden letterlijk uit Asperion overgenomen.
Claude (Opus 5.5) wordt alleen gebruikt voor tekst: aandachtspunten,
koppelvoorstellen voor nieuwe grootboekrekeningen en het eenmalig uitlezen
van de jaarrekening van vorig jaar bij een nieuwe klant.
"""
from pathlib import Path

BASIS = Path(__file__).resolve().parent.parent
TEMPLATES = BASIS / "templates"
KLANTEN = BASIS / "klanten"
KANTOOR_BESTAND = BASIS / "kantoor.json"
STANDAARD_MAPPING = BASIS / "standaard_mapping.json"


import threading as _threading

# pypdfium2 (PDFium) is niet thread-safe; Streamlit draait reruns in parallelle threads.
# Alle PDFium-gebruik loopt daarom via dit ene slot (anders: segfault).
PDFIUM_SLOT = _threading.RLock()
