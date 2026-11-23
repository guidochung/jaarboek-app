"""Signalen: door code berekende aandachtspunten (zonder AI).

Deze signalen zijn de enige invoer (samen met de kerncijfers) voor Claude, zodat
de AI-aanroep klein blijft en Claude geen bedragen hoeft te berekenen.
"""
from __future__ import annotations

from .berekenen import FOUT, LET_OP, Berekening
from .structuur import KOSTEN_POSTEN, LABELS, fmt  # noqa: F401

# Posten die we op mutaties bekijken (geen totalen die alleen optellingen zijn)
BALANS_POSTEN = ["mva", "debiteuren", "overige_vorderingen", "liquide", "ev", "lt", "kt_fin_lease",
                 "crediteuren", "belastingen", "otb_nog_te_betalen", "otb_vooruitontvangen"]
WV_POSTEN = ["omzet", "kostprijs", "brutomarge", *KOSTEN_POSTEN, "financieel", "resultaat"]
KERNCIJFERS = ["omzet", "kostprijs", "brutomarge", "totaal_kosten", "bedrijfsresultaat", "financieel", "resultaat",
               "mva", "vorderingen", "liquide", "ev", "lt", "kt", "activa", "passiva"]


def _pct(nu: int, vorig: int) -> float | None:
    return None if vorig == 0 else (nu - vorig) / abs(vorig) * 100


def bepaal_signalen(ber: Berekening, p: dict, kantoor: dict, labels: dict | None = None) -> list[dict]:
    """Lijst van {soort, ernst, titel, tekst, posten}. Tekst is door code geformuleerd."""
    c = ber.c
    labels = labels or LABELS
    drempel = int(kantoor.get("signaal_drempel_bedrag", 5000))
    drempel_pct = float(kantoor.get("signaal_drempel_procent", 25))
    s: list[dict] = []

    def signaal(soort, ernst, titel, tekst, posten=()):
        s.append({"soort": soort, "ernst": ernst, "titel": titel, "tekst": tekst, "posten": list(posten)})

    for k in ber.controles:
        if k["status"] in (LET_OP, FOUT):
            titel, tekst = k.get("kort") or (k["controle"], "Los dit op voordat je vaststelt.")
            signaal("controle", "hoog" if k["status"] == FOUT else "laag", titel, tekst)

    for post in BALANS_POSTEN + WV_POSTEN:
        nu, vorig = c.get(post, 0), p.get(post, 0)
        naam = labels.get(post, post)
        if nu == 0 and abs(vorig) >= drempel:
            signaal("nihil", "middel", f"{naam} nihil", f"Vorig jaar {fmt(vorig)}. Navragen of dit compleet is.", [post])
            continue
        pct = _pct(nu, vorig)
        if abs(nu - vorig) >= drempel and (pct is None or abs(pct) >= drempel_pct):
            titel = f"{naam} {pct:+.0f}%" if pct is not None else f"{naam} {'gestegen' if nu > vorig else 'gedaald'}"
            signaal("mutatie", "laag", titel, f"Van {fmt(vorig)} naar {fmt(nu)}.", [post])

    if c.get("omzet") and p.get("omzet"):
        bm, bm_v = c["brutomarge"] / c["omzet"] * 100, p["brutomarge"] / p["omzet"] * 100
        if abs(bm - bm_v) >= 2:
            signaal("marge", "middel", "Brutomarge verandert",
                    f"{bm:.1f}% van de omzet (vorig jaar {bm_v:.1f}%).", ["brutomarge"])

    ev = ber.ev_verloop
    if ev and -ev["prive"] > ev["resultaat"]:
        signaal("vermogen", "middel", "Privé-opnamen boven resultaat",
                f"Eigen vermogen daalt van {fmt(ev['begin'])} naar {fmt(ev['eind'])}.", ["ev"])
    if c.get("ev", 0) < 0:
        signaal("vermogen", "hoog", "Eigen vermogen negatief", f"{fmt(c['ev'])}; continuïteit beoordelen.", ["ev"])

    if ber.lease:
        signaal("lease", "laag", "Kortlopend deel lease geschat",
                "Gelijk aan de aflossing dit jaar; controleer met het aflossingsschema.", ["kt_fin_lease"])
        for naam, l in ber.lease.items():
            if l["saldo_eind"] and l["saldo_eind"] <= l["aflossing_boekjaar"]:
                signaal("lease", "laag", f"{naam} loopt af", f"Restschuld {fmt(l['saldo_eind'])} is volledig kortlopend.")
    return s


def samenvatting_voor_ai(ber: Berekening, p: dict, signalen: list[dict], jaar: int, labels: dict | None = None) -> dict:
    """Compacte invoer voor Claude (~1-2K tokens): kerncijfers + signalen, geen rekeningen."""
    labels = labels or LABELS
    c = ber.c
    return {
        "boekjaar": jaar,
        "kerncijfers": {labels.get(k, k): {"boekjaar": c.get(k, 0), "vorig_jaar": p.get(k, 0)} for k in KERNCIJFERS},
        "eigen_vermogen_verloop": {"begin": ber.ev_verloop.get("begin"), "resultaat": ber.ev_verloop.get("resultaat"),
                                   "prive": ber.ev_verloop.get("prive"), "eind": ber.ev_verloop.get("eind")},
        "signalen": [{"ernst": x["ernst"], "titel": x["titel"], "tekst": x["tekst"]} for x in signalen],
    }


def aandachtspunten_uit_signalen(signalen: list[dict], maximum: int = 5) -> list[dict]:
    """Terugval zonder AI: de belangrijkste signalen als aandachtspunten."""
    volgorde = {"hoog": 0, "middel": 1, "laag": 2}
    return [{"titel": x["titel"], "toelichting": x["tekst"], "ernst": x["ernst"]}
            for x in sorted(signalen, key=lambda x: volgorde[x["ernst"]])][:maximum]
