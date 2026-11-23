#!/usr/bin/env python3
"""
Proefrun met de echte Claude API (Opus 5.5): meet tokens en kosten.

Werkt op een KOPIE van de dossiers (je echte klantmappen worden niet aangepast).
Geschatte kosten van deze proefrun: ongeveer $0,10 - $0,60.

Gebruik (sleutel alleen in deze terminalsessie):
  export ANTHROPIC_API_KEY=sk-ant-...
  python3 -P proefrun_api.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

BASIS = Path(__file__).resolve().parent
sys.path.insert(0, str(BASIS))

from jaarboek.ai import ClaudeAI  # noqa: E402
from jaarboek.dossier import Klant, laad_kantoor  # noqa: E402
from jaarboek.onboarding import bewaar_als_klant, lees_vorige_jaarrekening  # noqa: E402
from jaarboek.pipeline import maak_concept  # noqa: E402

DATA = BASIS / "tests" / "data"


def regel(g):
    return (f"  {g.taak:<24} input {g.input_tokens:>6}  output {g.output_tokens:>6}  "
            f"${g.kosten_usd:.4f}  (model {g.model})")


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Zet eerst ANTHROPIC_API_KEY (zie de uitleg bovenin dit bestand).")
        return 2
    kantoor = laad_kantoor()
    tmp = Path(tempfile.mkdtemp(prefix="jaarboek_proefrun_"))
    shutil.copytree(BASIS / "klanten" / "misto_interiors", tmp / "misto_interiors",
                    ignore=shutil.ignore_patterns("output", "ai_gebruik.jsonl"))

    print("1) Concept MISTO 2025 met Claude-aandachtspunten (gewone run: alle rekeningen bekend)")
    ai1 = ClaudeAI(effort=kantoor.get("ai_effort", "medium"))
    c = maak_concept(Klant(tmp / "misto_interiors"), DATA / "misto_balans_2025.xlsx", date.today(), kantoor,
                     ai=ai1, uitmap=tmp / "uit1")
    for g in c.gebruik:
        print(regel(g))
    print(f"  bron aandachtspunten: {c.aandachtspunten_bron}; waarschuwingen: {c.waarschuwingen or 'geen'}")
    for a in c.aandachtspunten:
        extra = f"  [CONTROLEER bedragen {a['onbekende_bedragen']}]" if a.get("onbekende_bedragen") else ""
        print(f"   - [{a['ernst']}] {a['titel']}: {a['toelichting']}{extra}")

    print("\n2) Nieuwe klant: jaarrekening 2024 (PDF) uitlezen + eerste concept met koppelvoorstellen")
    ai2 = ClaudeAI(effort=kantoor.get("ai_effort", "medium"))
    inl = lees_vorige_jaarrekening(DATA / "misto_jaarrekening_2024.pdf", ai2)
    for k in inl.controles:
        print(f"  [{k['status']}] {k['controle']}: {k['detail']}")
    inl.naam = inl.naam + " (proefrun)"
    nieuw = bewaar_als_klant(inl, root=tmp / "nieuw")
    c2 = maak_concept(nieuw, DATA / "misto_balans_2025.xlsx", date.today(), kantoor, ai=ai2, uitmap=tmp / "uit2")
    for g in ai2.gebruik:
        print(regel(g))
    for k in c2.ai_koppelingen:
        print(f"  koppeling: {k['nr']} {k['naam']} -> {k['label']} ({k['zekerheid']})")
    print(f"  controles: {c2.status_telling}")
    gelijk = all(c2.c[k] == c.c[k] for k in c.c if k in c2.c)
    print(f"  cijfers gelijk aan route 1: {gelijk}")

    t1, t2 = ai1.totaal, ai2.totaal
    print(f"\nKosten gewone run per concept:      ${t1['kosten_usd']:.4f}  ({t1['input_tokens']} in / {t1['output_tokens']} uit)")
    print(f"Kosten nieuwe klant (eenmalig):     ${t2['kosten_usd']:.4f}  ({t2['input_tokens']} in / {t2['output_tokens']} uit)")
    print(f"\nUitvoer staat in {tmp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
