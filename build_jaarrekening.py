#!/usr/bin/env python3
"""
Jaarrekening-generator, opdrachtregelversie (de app gebruikt dezelfde code).

Voorbeelden:
  python3 -P build_jaarrekening.py --klant misto_interiors --balans ~/Downloads/Balans_WV_Report_9_11_2026.xlsx
  python3 -P build_jaarrekening.py --klant misto_interiors --balans balans.pdf --datum 2026-11-16 --ai
  python3 -P build_jaarrekening.py --klant misto_interiors --balans balans.xlsx --vaststellen

--ai gebruikt Claude Opus 5.5 voor aandachtspunten en nieuwe koppelingen (sleutel uit ANTHROPIC_API_KEY).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jaarboek import KLANTEN  # noqa: E402
from jaarboek.ai import ClaudeAI  # noqa: E402
from jaarboek.dossier import Klant, laad_kantoor  # noqa: E402
from jaarboek.pipeline import STAPPEN, maak_concept, stel_vast  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--klant", required=True, help="map in klanten/, bv. misto_interiors")
    ap.add_argument("--balans", required=True, type=Path, help="Asperion balans/W&V als .xlsx of .pdf")
    ap.add_argument("--datum", type=date.fromisoformat, default=date.today(), help="datum samenstelling (JJJJ-MM-DD)")
    ap.add_argument("--ai", action="store_true", help="Claude Opus 5.5 gebruiken")
    ap.add_argument("--vaststellen", action="store_true", help="ook de definitieve versie maken en opslaan")
    args = ap.parse_args()

    klant = Klant(KLANTEN / args.klant)
    if not (klant.map / "klant.json").exists():
        print(f"Klant '{args.klant}' niet gevonden in {KLANTEN}", file=sys.stderr)
        return 2
    kantoor = laad_kantoor()
    ai = ClaudeAI(effort=kantoor.get("ai_effort", "medium")) if args.ai else None

    def voortgang(stap, detail=""):
        if detail:
            print(f"  {STAPPEN[stap]}: {detail}")

    concept = maak_concept(klant, args.balans.expanduser(), args.datum, kantoor, ai=ai, voortgang=voortgang)
    print(f"\nPDF:     {concept.pdf}\nRapport: {concept.rapport}")
    for k in concept.controles:
        print(f"  [{k['status']}] {k['controle']}: {k['detail']}")
    print("\nAandachtspunten:")
    for a in concept.aandachtspunten:
        print(f"  [{a['ernst']}] {a['titel']}: {a['toelichting']}")
    if concept.gebruik:
        t = sum(g.input_tokens for g in concept.gebruik), sum(g.output_tokens for g in concept.gebruik)
        print(f"\nClaude: {t[0]} input- en {t[1]} output-tokens, ${concept.kosten_usd:.4f}")
    if args.vaststellen:
        print(f"\nDefinitief: {stel_vast(concept)}")
    return 1 if concept.status_telling["FOUT"] else 0


if __name__ == "__main__":
    sys.exit(main())
