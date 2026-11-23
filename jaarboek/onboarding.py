"""Oud jaarverslag (PDF) uitlezen tot een dossier, met ALLE jaarkolommen.

Een jaarverslag kan 2 of meer jaarkolommen hebben (verslagjaar + vergelijkende jaren).
Claude leest de (platte) tekst uit; code controleert daarna per jaarkolom:
  1. elk overgenomen bedrag komt letterlijk voor in de PDF (geen verzonnen bedragen);
  2. totalen tellen op (met 2 euro marge voor afronding in het gepubliceerde verslag).
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path

from .berekenen import FOUT, LET_OP, OK
from .dossier import Klant, nieuwe_klant, slug, standaard_mapping
from .structuur import ALLE_POSTEN, KOSTEN_POSTEN, LABELS, WV_STRUCTUUR, fmt

# Posten die gelijk zijn aan een andere post in het model van het kantoor (worden afgeleid)
AFGELEID = {
    "debiteuren": "deb_debiteuren", "overige_vorderingen": "ov_vooruitbetalingen", "kruisposten": "krp_stortingen",
    "ev": "kapitaal", "lt_fin_lease": "lt", "belastingen": "btw_omzetbelasting", "omzet_omzet": "omzet",
    "inkopen": "kostprijs", "afschr_mva": "afschrijvingen", "rb_bank": "rentebaten", "rl_financieringen": "rentelasten",
}
# Extra uitleg voor Claude waar het label alleen dubbelzinnig is
OMSCHRIJVING = {
    "mva": "Materiële vaste activa (totaal)", "vorderingen": "Vorderingen (totaal)",
    "deb_debiteuren": "Debiteuren", "ov_vooruitbetalingen": "Overige vorderingen / vooruitbetalingen",
    "tegoeden": "Tegoeden op bankrekeningen (totaal)", "liquide": "Liquide middelen (totaal)",
    "vlottend": "Vlottende activa (totaal)", "activa": "Totaal activa", "kapitaal": "Eigen vermogen / kapitaal",
    "lt": "Langlopende schulden (totaal)", "kt_fin_lease": "Financial leaseverplichtingen onder kortlopende schulden",
    "kt": "Kortlopende schulden (totaal)", "passiva": "Totaal passiva",
    "overlopende": "Overlopende passiva (totaal)", "btw_omzetbelasting": "Belastingen en premies / omzetbelasting",
    "kostprijs": "Kostprijs van de omzet / inkopen", "rentebaten": "Rentebaten en soortgelijke opbrengsten (positief)",
    "rentelasten": "Rentelasten en soortgelijke kosten (positief)",
    "financieel": "Financiële baten en lasten (saldo, met teken)", "totaal_kosten": "Totaal kosten",
}
VRAAG_POSTEN = [k for k in ALLE_POSTEN if k not in AFGELEID]


@dataclass
class Inlezing:
    naam: str
    jaren: list[int]               # jaartallen van de kolommen, nieuwste eerst
    kolommen: list[dict]           # per jaarkolom alle posten (incl. lease_<x>_lt)
    gegevens: dict                 # vestigingsplaats, kvk, afschrijvingen_tekst, lease_teksten
    leasecontracten: dict          # post -> {lt_post, label}
    controles: list[dict] = field(default_factory=list)
    tekst: str = ""                # tekst van de PDF, voor hercontrole na handmatige correcties

    @property
    def jaar(self) -> int:
        return self.jaren[0] if self.jaren else 0

    @jaar.setter
    def jaar(self, waarde: int) -> None:
        verschil = int(waarde) - self.jaar
        self.jaren = [j + verschil for j in self.jaren] if self.jaren else [int(waarde)]

    @property
    def posten(self) -> dict:
        """Posten van het verslagjaar (de eerste kolom)."""
        return self.kolommen[0]

    @property
    def fouten(self) -> int:
        return sum(1 for k in self.controles if k["status"] == FOUT)


def pdf_tekst(pad: Path) -> str:
    """Tekst van het jaarverslag zonder het CONCEPT-watermerk (letters > 50pt)."""
    import pdfplumber
    delen = []
    with pdfplumber.open(pad) as pdf:
        for i, pg in enumerate(pdf.pages, 1):
            pg = pg.filter(lambda o: not (o.get("object_type") == "char" and o.get("size", 0) > 50))
            delen.append(f"[pagina {i}]\n{pg.extract_text(x_tolerance=1.0) or ''}")
    return "\n".join(delen)


def _bedragen_in_pdf(tekst: str) -> set[str]:
    return set(re.findall(r"-?\d{1,3}(?:\.\d{3})*", tekst))


def _optellingen(p: dict, lease_lt: dict) -> list[tuple[str, int, int]]:
    sommen = [
        ("Materiële vaste activa", "mva", ["mva_inventaris", "mva_vervoer"]),
        ("Vorderingen", "vorderingen", ["deb_debiteuren", "ov_vooruitbetalingen"]),
        ("Tegoeden op bankrekeningen", "tegoeden", ["bank_rc", "bank_spaar"]),
        ("Liquide middelen", "liquide", ["tegoeden", "krp_stortingen"]),
        ("Vlottende activa", "vlottend", ["vorderingen", "liquide"]),
        ("Totaal activa", "activa", ["mva", "vlottend"]),
        ("Overlopende passiva", "overlopende", ["otb_nog_te_betalen", "otb_vooruitontvangen"]),
        ("Kortlopende schulden", "kt", ["kt_fin_lease", "btw_omzetbelasting", "crediteuren", "overlopende"]),
        ("Totaal passiva", "passiva", ["kapitaal", "lt", "kt"]),
        ("Totaal kosten", "totaal_kosten", KOSTEN_POSTEN),
    ] + [(LABELS[h], h, r) for h, r in WV_STRUCTUUR.items() if len(r) > 1]
    regels = [(label, p.get(tot, 0), sum(p.get(d, 0) for d in delen)) for label, tot, delen in sommen]
    if lease_lt:
        regels.append(("Langlopende schulden = som leasecontracten", p.get("lt", 0), sum(lease_lt.values())))
    regels += [
        ("Activa = passiva", p.get("activa", 0), p.get("passiva", 0)),
        ("Brutomarge = omzet - kostprijs", p.get("brutomarge", 0), p.get("omzet", 0) - p.get("kostprijs", 0)),
        ("Bedrijfsresultaat = brutomarge - kosten", p.get("bedrijfsresultaat", 0),
         p.get("brutomarge", 0) - p.get("totaal_kosten", 0)),
        ("Financieel = rentebaten - rentelasten", p.get("financieel", 0), p.get("rentebaten", 0) - p.get("rentelasten", 0)),
        ("Resultaat = bedrijfsresultaat + financieel", p.get("resultaat", 0),
         p.get("bedrijfsresultaat", 0) + p.get("financieel", 0)),
    ]
    return regels


def valideer(kolommen: list[dict], jaren: list[int], lease_posten: list[str], tekst: str) -> list[dict]:
    ctrl: list[dict] = []
    in_pdf = _bedragen_in_pdf(tekst)
    niet_gevonden, aantal = [], 0
    for jaar, p in zip(jaren, kolommen):
        for k, v in p.items():
            if k in AFGELEID or v == 0:
                continue
            aantal += 1
            if fmt(v) not in in_pdf:
                niet_gevonden.append(f"{LABELS.get(k, k)} {jaar} ({fmt(v)})")
    ctrl.append({"status": FOUT if niet_gevonden else OK,
                 "controle": "Elk overgenomen bedrag komt letterlijk voor in de PDF",
                 "detail": (f"NIET GEVONDEN: {', '.join(niet_gevonden)}" if niet_gevonden
                            else f"{aantal} bedragen in {len(kolommen)} jaarkolommen gecontroleerd")})

    for jaar, p in zip(jaren, kolommen):
        lease_lt = {k: p.get(k, 0) for k in lease_posten}
        regels = _optellingen(p, lease_lt)
        fout = [f"{l}: {fmt(a)} vs {fmt(b)}" for l, a, b in regels if abs(a - b) > 2]
        afr = [l for l, a, b in regels if 0 < abs(a - b) <= 2]
        ctrl.append({"status": FOUT if fout else OK, "controle": f"Totalen {jaar} tellen op (marge 2 euro)",
                     "detail": (f"AFWIJKEND: {'; '.join(fout)}" if fout else f"{len(regels)} optellingen gecontroleerd")
                     + (f"; afrondingsverschil van 1-2 euro in: {', '.join(afr)}" if afr else "")})
    if kolommen and (not kolommen[0].get("activa") or not kolommen[0].get("resultaat")):
        ctrl.append({"status": LET_OP, "controle": "Balanstotaal en resultaat gevonden",
                     "detail": "Totaal activa of resultaat is 0; is dit wel een volledig jaarverslag?"})
    return ctrl


def _vul_afgeleid(p: dict) -> None:
    for kopie, bron in AFGELEID.items():
        p[kopie] = p.get(bron, 0)


def lees_vorige_jaarrekening(pdf_pad: Path, ai) -> Inlezing:
    tekst = pdf_tekst(pdf_pad)
    catalogus = {k: OMSCHRIJVING.get(k, LABELS.get(k, k)) for k in VRAAG_POSTEN}
    data = ai.lees_jaarrekening(tekst, catalogus)

    jaren = [int(j) for j in data.get("jaren", [])] or [int(data.get("boekjaar", 0))]
    n = len(jaren)
    kolommen = [{k: 0 for k in ALLE_POSTEN} for _ in range(n)]
    for item in data.get("posten", []):
        if item["post"] in kolommen[0] and item["post"] not in AFGELEID:
            for i, bedrag in enumerate(item.get("bedragen", [])[:n]):
                kolommen[i][item["post"]] = int(bedrag)

    leasecontracten, gebruikt = {}, set()
    for lc in data.get("leasecontracten", []):
        basis = "lease_" + slug(lc["naam"])[:30]
        post, i = basis, 2
        while post in gebruikt:
            post, i = f"{basis}_{i}", i + 1
        gebruikt.add(post)
        leasecontracten[post] = {"lt_post": f"{post}_lt", "label": lc["naam"]}
        bedragen = list(lc.get("langlopend", [])) + [0] * n
        for j in range(n):
            kolommen[j][f"{post}_lt"] = int(bedragen[j])
    for p in kolommen:
        _vul_afgeleid(p)

    gegevens = {
        "vestigingsplaats": data.get("vestigingsplaats", ""),
        "kvk": data.get("kvk", ""),
        "afschrijvingen_tekst": data.get("afschrijvingen_tekst", []),
        "lease_teksten": [lc["tekst"] for lc in data.get("leasecontracten", []) if lc.get("tekst")],
    }
    inl = Inlezing(naam=data.get("bedrijfsnaam", "").strip(), jaren=jaren, kolommen=kolommen,
                   gegevens=gegevens, leasecontracten=leasecontracten, tekst=tekst)
    hercontroleer(inl)
    return inl


def hercontroleer(inl: Inlezing) -> list[dict]:
    for p in inl.kolommen:
        _vul_afgeleid(p)
    lease_posten = [spec["lt_post"] for spec in inl.leasecontracten.values()]
    inl.controles = valideer(inl.kolommen, inl.jaren, lease_posten, inl.tekst)
    return inl.controles


def bewaar_als_klant(inl: Inlezing, root: Path | None = None, overschrijf: bool = False) -> Klant:
    if not inl.naam or not inl.jaar:
        raise ValueError("Bedrijfsnaam en boekjaar zijn verplicht.")
    mapping = copy.deepcopy(standaard_mapping())
    mapping["leasecontracten"] = inl.leasecontracten
    mapping["ai_voorstellen"] = {}
    vergelijkend = [{"jaar": j, "posten": p} for j, p in zip(inl.jaren[1:], inl.kolommen[1:])]
    kwargs = {"root": root} if root else {}
    return nieuwe_klant(inl.naam, inl.gegevens, mapping, inl.jaar, inl.posten, overschrijf=overschrijf,
                        vergelijkend=vergelijkend, **kwargs)
