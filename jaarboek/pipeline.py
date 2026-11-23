"""De knop 'Maak concept': balans -> concept-jaarrekening (PDF + controlerapport)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .ai import AIFout, ClaudeAI, Gebruik
from .berekenen import LET_OP, Berekening, KoppelFout, bereken, niet_gekoppeld
from .dossier import Klant
from .inlezen import boekjaar, lees_asperion
from .rapport import schrijf_rapport
from .render import render_pdf
from .signalen import aandachtspunten_uit_signalen, bepaal_signalen, samenvatting_voor_ai
from .structuur import BALANS_REKENINGPOSTEN, WV_REKENINGPOSTEN, datum_nl, labels_voor

STAPPEN = {
    "inlezen": "Balans inlezen",
    "dossier": "Dossier vorig jaar laden",
    "koppelingen": "Grootboekrekeningen koppelen",
    "berekenen": "Jaarrekening berekenen en controleren",
    "aandachtspunten": "Aandachtspunten bepalen",
    "pdf": "PDF en controlerapport maken",
}


class DossierFout(Exception):
    pass


@dataclass
class Concept:
    klant: Klant
    jaar: int
    datum: str
    c: dict
    p: dict
    berekening: Berekening
    signalen: list[dict]
    aandachtspunten: list[dict]
    aandachtspunten_bron: str          # "ai" of "code"
    ai_koppelingen: list[dict]
    gebruik: list[Gebruik]
    labels: dict
    meta: dict
    asperion_meta: dict
    kolommen: list[dict] = field(default_factory=list)   # alle jaarkolommen, nieuwste eerst
    jaren: list[int] = field(default_factory=list)
    pdf: Path | None = None
    rapport: Path | None = None
    waarschuwingen: list[str] = field(default_factory=list)

    @property
    def controles(self) -> list[dict]:
        return self.berekening.controles

    @property
    def status_telling(self) -> dict:
        t = {"OK": 0, "LET OP": 0, "FOUT": 0}
        for k in self.controles:
            t[k["status"]] += 1
        return t

    @property
    def kosten_usd(self) -> float:
        return round(sum(g.kosten_usd for g in self.gebruik), 5)


def _koppel_opties(mapping: dict, labels: dict, deel: str) -> dict[str, str]:
    posten = BALANS_REKENINGPOSTEN + list(mapping.get("leasecontracten", {})) if deel == "balans" else WV_REKENINGPOSTEN
    return {p: labels.get(p, p) for p in posten}


def koppel_nieuwe_rekeningen(asp: dict, klant: Klant, ai: ClaudeAI | None) -> list[dict]:
    """Nieuwe grootboekrekeningen: Claude stelt een post voor, die wordt direct toegepast en gemarkeerd."""
    mapping = klant.mapping
    namen = {nr: r["naam"] for nr, r in asp["rekeningen"].items()}
    if any(mapping.get("rekeningnamen", {}).get(nr) != n for nr, n in namen.items()):
        mapping["rekeningnamen"] = {**mapping.get("rekeningnamen", {}), **namen}  # voor het koppelingenscherm
        klant.bewaar_mapping(mapping)
    nieuw = niet_gekoppeld(asp, mapping)
    if not nieuw:
        return []
    if ai is None:
        raise KoppelFout(nieuw)
    labels = labels_voor(mapping)
    toegepast = []
    for deel in ("balans", "wv"):
        groep = [r for r in nieuw if r["deel"] == deel]
        if not groep:
            continue
        voorbeelden: dict[str, list] = {}
        for r in groep:
            sleutel = f"{r['sectie']} / {r['groep']}"
            voorbeelden[sleutel] = [{"nr": nr, "naam": x["naam"], "post": mapping["rekeningen"][nr]}
                                    for nr, x in asp["rekeningen"].items()
                                    if x["deel"] == deel and x["groep"] == r["groep"] and nr in mapping["rekeningen"]][:8]
        for v in ai.koppelvoorstellen(groep, _koppel_opties(mapping, labels, deel), voorbeelden):
            mapping["rekeningen"][v["rekening"]] = v["post"]
            r = next(x for x in groep if x["nr"] == v["rekening"])
            mapping["ai_voorstellen"][v["rekening"]] = {
                "naam": r["naam"], "post": v["post"], "zekerheid": v["zekerheid"], "toelichting": v["toelichting"],
                "datum": datetime.now().isoformat(timespec="seconds"), "goedgekeurd": False}
            toegepast.append({"nr": v["rekening"], "naam": r["naam"], "post": v["post"],
                              "label": labels.get(v["post"], v["post"]), "zekerheid": v["zekerheid"],
                              "toelichting": v["toelichting"]})
    klant.bewaar_mapping(mapping)
    rest = niet_gekoppeld(asp, mapping)
    if rest:
        raise KoppelFout(rest)
    return toegepast


def maak_concept(klant: Klant, balans_pad: Path, datum: date, kantoor: dict, ai: ClaudeAI | None = None,
                 voortgang: Callable[[str, str], None] | None = None, uitmap: Path | None = None) -> Concept:
    stap = voortgang or (lambda s, d="": None)

    stap("inlezen", "")
    asp = lees_asperion(Path(balans_pad))
    jaar = boekjaar(asp)
    stap("inlezen", f"{len(asp['rekeningen'])} grootboekrekeningen, boekjaar {jaar}")

    stap("dossier", "")
    vj = klant.jaarrekening(jaar - 1)
    if vj is None:
        raise DossierFout(f"Geen jaarrekening {jaar - 1} in het dossier van {klant.naam}. "
                          f"Beschikbaar: {', '.join(map(str, klant.jaren())) or 'geen'}.")
    p = vj["posten"]
    # Aantal jaarkolommen aanhouden: nieuw jaar vooraan, oudste kolom valt weg.
    ouder = vj.get("vergelijkend", [])
    n = min(max(2, int(vj.get("aantal_jaren", 2))), 2 + len(ouder))
    stap("dossier", f"jaarrekening {jaar - 1} ({vj.get('status', 'onbekend')}), {n} jaarkolommen")

    stap("koppelingen", "")
    gebruik_start = len(ai.gebruik) if ai else 0
    ai_koppelingen = koppel_nieuwe_rekeningen(asp, klant, ai)
    stap("koppelingen", f"{len(ai_koppelingen)} nieuwe rekening(en) gekoppeld door Claude" if ai_koppelingen
         else "alle rekeningen waren al gekoppeld")

    stap("berekenen", "")
    mapping = klant.mapping
    labels = labels_voor(mapping)
    ber = bereken(asp, mapping, vj)
    open_ai = {nr: v for nr, v in mapping.get("ai_voorstellen", {}).items()
               if not v.get("goedgekeurd") and nr in asp["rekeningen"]}
    if open_ai:
        ber.controles.append({
            "status": LET_OP, "controle": "Koppelingen door Claude, nog niet goedgekeurd",
            "detail": "; ".join(f"{nr} {v['naam']} -> {labels.get(v['post'], v['post'])}" for nr, v in open_ai.items()),
            "kort": ("Nieuwe rekeningen gekoppeld", f"{len(open_ai)} door Claude gekoppeld; controleer de post.")})
    stap("berekenen", "{OK} OK, {LET OP} LET OP, {FOUT} FOUT".format(**_tel(ber.controles)))

    stap("aandachtspunten", "")
    signalen = bepaal_signalen(ber, p, kantoor, labels)
    waarschuwingen = []
    if ai is not None:
        try:
            punten = ai.aandachtspunten(samenvatting_voor_ai(ber, p, signalen, jaar, labels))
            bron = "ai"
        except AIFout as e:
            waarschuwingen.append(f"Claude niet beschikbaar ({e}); aandachtspunten zonder AI opgesteld.")
            punten, bron = aandachtspunten_uit_signalen(signalen), "code"
    else:
        punten, bron = aandachtspunten_uit_signalen(signalen), "code"
    stap("aandachtspunten", f"{len(punten)} punten ({'Claude Opus 5.5' if bron == 'ai' else 'zonder AI'})")

    stap("pdf", "")
    gegevens = klant.gegevens
    meta = {
        "bedrijf": gegevens["naam"], "jaar": jaar, "vorig_jaar": jaar - 1, "datum": datum_nl(datum),
        "jaren": [jaar, vj["jaar"]] + [v["jaar"] for v in ouder[:n - 2]], "n": n,
        "klant": gegevens, "kantoor": kantoor, "concept": True, "pagina_totaal": 21,
        "lease_lt_posten": [spec["lt_post"] for spec in mapping.get("leasecontracten", {}).values()],
    }
    gebruik = list(ai.gebruik[gebruik_start:]) if ai else []
    concept = Concept(klant=klant, jaar=jaar, datum=meta["datum"], c=ber.c, p=p, berekening=ber, signalen=signalen,
                      aandachtspunten=punten, aandachtspunten_bron=bron, ai_koppelingen=ai_koppelingen,
                      gebruik=gebruik, labels=labels, meta=meta, asperion_meta=asp["meta"],
                      waarschuwingen=waarschuwingen,
                      kolommen=[ber.c, p] + [v["posten"] for v in ouder[:n - 2]], jaren=meta["jaren"])
    uit = Path(uitmap) if uitmap else klant.output_map(jaar)
    concept.pdf = render_pdf(concept.kolommen, meta, labels, uit / f"{gegevens['naam']} Jaarrekening {jaar} (concept).pdf")
    concept.rapport = schrijf_rapport(concept, uit / f"controlerapport_{jaar}.md")
    klant.log_ai_gebruik([{**g.als_dict(), "klant": klant.id, "jaar": jaar} for g in gebruik])
    stap("pdf", concept.pdf.name)
    return concept


def stel_vast(concept: Concept, uitmap: Path | None = None) -> Path:
    """Definitieve versie: PDF zonder watermerk + cijfers in het dossier (vergelijkende cijfers volgend jaar)."""
    meta = {**concept.meta, "concept": False}
    uit = Path(uitmap) if uitmap else concept.klant.output_map(concept.jaar)
    pdf = render_pdf(concept.kolommen, meta, concept.labels, uit / f"{meta['bedrijf']} Jaarrekening {concept.jaar}.pdf")
    concept.klant.bewaar_jaarrekening(
        concept.jaar, concept.c, status="vastgesteld",
        bron=f"Asperion-export {concept.asperion_meta.get('exportdatum', '')}",
        vergelijkend=[{"jaar": j, "posten": k} for j, k in zip(concept.jaren[1:], concept.kolommen[1:])])
    return pdf


def _tel(controles):
    t = {"OK": 0, "LET OP": 0, "FOUT": 0}
    for k in controles:
        t[k["status"]] += 1
    return t
