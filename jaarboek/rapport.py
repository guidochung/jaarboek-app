"""Controlerapport (Markdown) bij een concept-jaarrekening."""
from __future__ import annotations

from pathlib import Path

from .structuur import fmt

_KERN = [("omzet", "Omzet"), ("brutomarge", "Brutomarge"), ("totaal_kosten", "Totaal kosten"),
         ("resultaat", "Resultaat"), ("mva", "Materiële vaste activa"), ("vorderingen", "Vorderingen"),
         ("liquide", "Liquide middelen"), ("ev", "Eigen vermogen"), ("lt", "Langlopende schulden"),
         ("kt", "Kortlopende schulden"), ("activa", "Balanstotaal")]


def schrijf_rapport(concept, pad: Path) -> Path:
    c, p, j, vj = concept.c, concept.p, concept.jaar, concept.jaar - 1
    ber = concept.berekening
    r = [f"# Controlerapport jaarrekening {j} — {concept.meta['bedrijf']}", "",
         f"Datum samenstelling {concept.datum}. Bron: Asperion-export ({concept.asperion_meta.get('exportdatum', '?')}), "
         f"jaarrekening {vj} (vergelijkende cijfers).", "",
         "## 1. Automatische controles", "", "| Status | Controle | Detail |", "|---|---|---|"]
    for k in concept.controles:
        status = f"**{k['status']}**" if k["status"] == "FOUT" else k["status"]
        r.append(f"| {status} | {k['controle']} | {k['detail']} |")

    bron = "Claude Opus 5.5" if concept.aandachtspunten_bron == "ai" else "code (zonder AI)"
    r += ["", f"## 2. Aandachtspunten voor de accountant ({bron})", ""]
    for a in concept.aandachtspunten:
        extra = (f" _(Let op: bedrag(en) {', '.join(a['onbekende_bedragen'])} komen niet uit de berekening; controleer.)_"
                 if a.get("onbekende_bedragen") else "")
        r.append(f"- **[{a['ernst']}] {a['titel']}.** {a['toelichting']}{extra}")
    if concept.ai_koppelingen:
        r += ["", "**Nieuwe grootboekrekeningen, gekoppeld door Claude (nog goed te keuren):**", ""]
        for k in concept.ai_koppelingen:
            r.append(f"- {k['nr']} {k['naam']} → {k['label']} (zekerheid {k['zekerheid']}): {k['toelichting']}")
    for w in concept.waarschuwingen:
        r.append(f"- _{w}_")

    r += ["", "## 3. Kerncijfers", "", f"| Post | {j} | {vj} | Verschil |", "|---|---:|---:|---:|"]
    for k, lab in _KERN:
        r.append(f"| {lab} | {fmt(c.get(k, 0))} | {fmt(p.get(k, 0))} | {fmt(c.get(k, 0) - p.get(k, 0))} |")

    ev = ber.ev_verloop
    r += ["", "## 4. Verloop eigen vermogen", "", "| | Bedrag |", "|---|---:|",
          f"| Stand 1 januari {j} | {fmt(ev['begin'])} |", f"| Resultaat {j} | {fmt(ev['resultaat'])} |"]
    for naam, bedrag in ev["prive_detail"].items():
        r.append(f"| {naam[:1].upper() + naam[1:]} | {fmt(bedrag)} |")
    r += [f"| **Stand 31 december {j}** | **{fmt(ev['eind'])}** |", ""]

    if ber.lease:
        r += ["## 5. Leaseverplichtingen: splitsing lang/kort", "",
              "Kortlopend deel = aflossing in het boekjaar (methode jaarrekening vorig jaar).", "",
              "| Contract | Saldo 1-1 | Saldo 31-12 | Aflossing boekjaar | Kortlopend | Langlopend |",
              "|---|---:|---:|---:|---:|---:|"]
        for naam, l in ber.lease.items():
            r.append(f"| {naam} | {fmt(l['saldo_begin'])} | {fmt(l['saldo_eind'])} | {fmt(l['aflossing_boekjaar'])} "
                     f"| {fmt(l['kortlopend'])} | {fmt(l['langlopend'])} |")
        r.append("")

    r += [f"## 6. Aansluiting Asperion-kolom {vj} met jaarrekening {vj}", "",
          "| Post | Jaarrekening | Asperion | Verschil |", "|---|---:|---:|---:|"]
    for a in ber.aansluiting:
        r.append(f"| {concept.labels.get(a['post'], a['post'])} | {fmt(a['jaarrekening'])} | {fmt(a['asperion'])} "
                 f"| {fmt(a['verschil'])} |")

    if concept.gebruik:
        r += ["", "## 7. AI-gebruik (Claude Opus 5.5)", "", "| Taak | Input-tokens | Output-tokens | Kosten |",
              "|---|---:|---:|---:|"]
        for g in concept.gebruik:
            r.append(f"| {g.taak} | {g.input_tokens:,} | {g.output_tokens:,} | ${g.kosten_usd:.4f} |".replace(",", "."))
        r.append(f"| **Totaal** | | | **${concept.kosten_usd:.4f}** |")
    pad = Path(pad)
    pad.write_text("\n".join(r) + "\n", encoding="utf-8")
    return pad
