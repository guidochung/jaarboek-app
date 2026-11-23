"""Eén stap: oud jaarverslag (PDF) + nieuwe balans (PDF/Excel) -> nieuw jaarverslag.

Gebruikt dezelfde, geteste onderdelen als de uitgebreide app, maar zonder klantbeheer:
alles gebeurt in een werkmap.
  1. Claude leest het oude jaarverslag uit; code controleert elk bedrag tegen de PDF.
  2. De nieuwe balans wordt ingelezen; nieuwe grootboekrekeningen koppelt Claude.
  3. Code berekent het nieuwe jaarverslag en maakt de PDF (concept + zonder watermerk).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from .ai import ClaudeAI
from .berekenen import FOUT
from .inlezen import boekjaar, lees_asperion
from .onboarding import Inlezing, bewaar_als_klant, lees_vorige_jaarrekening
from .pipeline import Concept, maak_concept, stel_vast


class JaarverslagFout(Exception):
    pass


@dataclass
class Resultaat:
    inlezing: Inlezing
    concept: Concept
    definitief: Path


def maak_jaarverslag(oud_pdf: Path, balans: Path, datum: date, kantoor: dict, ai: ClaudeAI, werkmap: Path,
                     voortgang: Callable[[str, str], None] | None = None,
                     inlezing: Inlezing | None = None) -> Resultaat:
    """inlezing: een eerder uitgelezen oud jaarverslag hergebruiken (scheelt een Claude-verzoek)."""
    stap = voortgang or (lambda s, d="": None)
    werkmap = Path(werkmap)

    stap("balans", "")
    asp = lees_asperion(Path(balans))           # eerst de balans: goedkoop, en controleert het jaar
    jaar = boekjaar(asp)
    stap("balans", f"{len(asp['rekeningen'])} grootboekrekeningen, boekjaar {jaar}")

    stap("oud", "")
    if inlezing is None:
        inlezing = lees_vorige_jaarrekening(Path(oud_pdf), ai)
    if inlezing.jaar != jaar - 1:
        raise JaarverslagFout(f"Het oude jaarverslag is van {inlezing.jaar}, maar de balans is van {jaar}. "
                              f"Upload het jaarverslag {jaar - 1}.")
    fouten = [k for k in inlezing.controles if k["status"] == FOUT]
    if fouten:
        raise JaarverslagFout("Het oude jaarverslag kon niet betrouwbaar worden uitgelezen: "
                              + "; ".join(f"{k['controle']} ({k['detail']})" for k in fouten))
    stap("oud", f"{inlezing.naam}, jaarverslag {inlezing.jaar}: alle bedragen gecontroleerd")

    klant = bewaar_als_klant(inlezing, root=werkmap / "dossier", overschrijf=True)
    uit = werkmap / "uitvoer"
    concept = maak_concept(klant, Path(balans), datum, kantoor, ai=ai, uitmap=uit,
                           voortgang=lambda s, d="": stap(s, d))
    definitief = stel_vast(concept, uitmap=uit)
    return Resultaat(inlezing=inlezing, concept=concept, definitief=definitief)
