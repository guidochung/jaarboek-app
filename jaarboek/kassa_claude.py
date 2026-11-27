"""Vangnet voor onbekende kassaformaten: Claude leest het rapport, code controleert.

Claude krijgt de tekst van de bestanden (PDF met kolomopmaak, Excel als tabeltekst) en zet die om naar
het tussenformaat (`Kassaomzet`). Daarna gelden harde controles, zodat een leesfout nooit ongemerkt in de
boeking komt:
- elk bedrag dat Claude niet als 'afgeleid' meldt, moet letterlijk in het rapport staan (anders FOUT);
- de omzetregels moeten per tarief optellen tot het btw-overzicht van het rapport (in maak_boeking);
- betalingen min omzet, fooi en overige inkomsten = verschil (in maak_boeking).
Afgeleide bedragen en een btw-overzicht dat Claude zelf heeft uitgerekend krijgen LET OP.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from .omzet import (NUL, Betaalpost, Kassaomzet, OmzetFout, Omzetpost, controle, euro_centen, euro_nl,
                    soort_betaling)

MAX_TEKENS = 300_000                 # ± 80.000 tokens; grotere rapporten eerst inkorten

GETAL = {"type": "number"}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["kassasysteem", "bedrijf", "periode_van", "periode_tot", "omzet", "betalingen", "fooi",
                 "overige_inkomsten", "rapport_totalen", "niet_geboekt", "opmerkingen"],
    "properties": {
        "kassasysteem": {"type": "string"},
        "bedrijf": {"type": "string"},
        "periode_van": {"type": "string"}, "periode_tot": {"type": "string"},
        "omzet": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["groep", "tarief", "incl_btw", "btw", "afgeleid"],
            "properties": {"groep": {"type": "string"}, "tarief": {"type": "integer"},
                           "incl_btw": GETAL, "btw": GETAL, "afgeleid": {"type": "boolean"}}}},
        "betalingen": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["betaalwijze", "bedrag", "afgeleid"],
            "properties": {"betaalwijze": {"type": "string"}, "bedrag": GETAL, "afgeleid": {"type": "boolean"}}}},
        "fooi": GETAL, "overige_inkomsten": GETAL,
        "rapport_totalen": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["tarief", "incl_btw", "btw", "afgeleid"],
            "properties": {"tarief": {"type": "integer"}, "incl_btw": GETAL, "btw": GETAL,
                           "afgeleid": {"type": "boolean"}}}},
        "niet_geboekt": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["omschrijving", "bedrag"],
            "properties": {"omschrijving": {"type": "string"}, "bedrag": GETAL}}},
        "opmerkingen": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = """Je zet het maandrapport van een horeca-kassasysteem om naar gegevens voor één omzetboeking in de \
boekhouding. Regels:
- omzet: één regel per OMZETCATEGORIE van het rapport (niet per product) en per btw-tarief; een categorie met \
omzet in twee tarieven krijgt twee regels. Heeft het rapport geen categorieën, of tellen de categorieën niet op \
tot het btw-overzicht van het rapport, maak dan één regel per tarief met groep "Omzet <tarief>%" en de bedragen \
uit het btw-overzicht. incl_btw = bedrag inclusief btw, btw = btw-bedrag (0 als het er niet staat).
- Neem bedragen letterlijk over uit het rapport. Moet je een bedrag uitrekenen (optellen van dagen, een splitsing \
naar tarief), zet dan afgeleid=true. Verzin nooit bedragen.
- Kortingen, fooi, statiegeld en andere posten die volgens het rapport geen omzet zijn, horen niet in omzet.
- betalingen: per betaalwijze het ontvangen bedrag, op het laagste detailniveau (geen subtotalen naast hun \
uitsplitsing). Gebruik de namen uit het rapport.
- fooi: alleen fooi die in de betalingen hierboven zit (bovenop de omzet betaald). Staat fooi los van de \
betalingen, zet fooi op 0 en noem hem bij niet_geboekt. overige_inkomsten: idem voor overige inkomsten.
- rapport_totalen: het btw-overzicht van het rapport per tarief (incl. btw en btw). Geeft het rapport geen \
totalen per tarief, reken ze uit en zet afgeleid=true.
- niet_geboekt: andere bedragen die een boekhouder moet zien maar die niet in omzet of betalingen zitten.
- periode_van/periode_tot: kalenderdatums JJJJ-MM-DD. Loopt de kassadag door tot de volgende dag (bijv. \
12:00 tot 12:00), dan is periode_tot de laatste dag van de maand.
- kassasysteem: de titel of kop waaraan je dit soort rapport herkent.
- opmerkingen: maximaal 5 korte punten in het Nederlands over twijfels of dingen die de boekhouder moet weten."""


# --------------------------------------------------------------------------
# Bestanden -> tekst
# --------------------------------------------------------------------------
def _pdf_tekst(data: bytes, naam: str) -> str:
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(p.extract_text(layout=True) or "" for p in pdf.pages)
    except Exception as e:                               # noqa: BLE001
        raise OmzetFout(f"{naam}: kan deze PDF niet openen ({e}).") from None


def _cel(w) -> str:
    if w is None:
        return ""
    if isinstance(w, float):
        return f"{w:.2f}" if w != int(w) else str(int(w))
    if isinstance(w, (datetime, date)):
        return w.isoformat()[:10]
    return " ".join(str(w).split())


def _excel_tekst(data: bytes, naam: str) -> str:
    import openpyxl
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
            delen = []
            for ws in wb.worksheets:
                rijen = [" | ".join(_cel(c) for c in r).rstrip(" |") for r in ws.iter_rows(values_only=True)]
                delen.append(f"--- blad {ws.title} ---\n" + "\n".join(r for r in rijen if r.strip(" |")))
            wb.close()
    except Exception as e:                               # noqa: BLE001
        raise OmzetFout(f"{naam}: kan dit Excel-bestand niet openen ({e}).") from None
    return "\n".join(delen)


def tekst_van_bestanden(bestanden) -> str:
    delen = []
    for naam, data in bestanden:
        data = bytes(data)
        if data[:5] == b"%PDF-":
            tekst = _pdf_tekst(data, naam)
        elif data[:2] == b"PK":
            tekst = _excel_tekst(data, naam)
        else:
            tekst = data.decode("utf-8-sig", errors="replace")
        if not tekst.strip():
            raise OmzetFout(f"{naam}: geen tekst gevonden (is het een scan? Dan kan ook Claude het hier niet lezen).")
        delen.append(f"=== BESTAND: {naam} ===\n{tekst}")
    tekst = "\n\n".join(delen)
    if len(tekst) > MAX_TEKENS:
        raise OmzetFout(f"De rapporten zijn te groot om door Claude te laten lezen ({len(tekst):,} tekens). "
                        "Upload alleen de maandtotalen.".replace(",", "."))
    return tekst


# --------------------------------------------------------------------------
# Antwoord -> tussenformaat, met controles
# --------------------------------------------------------------------------
def _d(x) -> Decimal:
    try:
        return euro_centen(Decimal(str(x)))
    except (InvalidOperation, ValueError):
        raise OmzetFout(f"Claude gaf een ongeldig bedrag terug ('{x}').") from None


def _iso(s: str, wat: str) -> date:
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        raise OmzetFout(f"Claude gaf een ongeldige {wat} terug ('{s}').") from None


def _varianten(x: Decimal) -> set[str]:
    """Schrijfwijzen van een bedrag in een rapport: 1234.56, 1,234.56, 1.234,56, 1234,56."""
    en = f"{abs(x):,.2f}"
    nl = en.replace(",", "_").replace(".", ",").replace("_", ".")
    return {en.replace(",", ""), en, nl, nl.replace(".", "")}


def staat_in(x: Decimal, schoon: str) -> bool:
    """Staat het bedrag als los getal in de tekst (niet als stuk van een groter getal)?"""
    return any(re.search(rf"(?<![\d.,]){re.escape(v)}(?![\d]|[.,]\d)", schoon) for v in _varianten(x))


def naar_kassaomzet(r: dict, tekst: str, namen: list[str]) -> Kassaomzet:
    van, tot = _iso(r["periode_van"], "begindatum"), _iso(r["periode_tot"], "einddatum")
    if van.day == 1 and tot.day == 1 and tot > van and (tot - van).days >= 27:
        tot -= timedelta(days=1)                         # 1-9 t/m 1-10 (kassadag tot de volgende dag) = september
    omzet = [Omzetpost(str(o["groep"]).strip() or f"Omzet {o['tarief']}%", int(o["tarief"]), _d(o["incl_btw"]),
                       _d(o["btw"]), bool(o["afgeleid"])) for o in r["omzet"]]
    betalingen = [Betaalpost(str(b["betaalwijze"]).strip(), _d(b["bedrag"]), soort_betaling(b["betaalwijze"]))
                  for b in r["betalingen"] if _d(b["bedrag"])]
    totalen = {int(t["tarief"]): (_d(t["incl_btw"]), _d(t["btw"])) for t in r["rapport_totalen"]}
    niet = [(str(n["omschrijving"]), _d(n["bedrag"])) for n in r["niet_geboekt"] if _d(n["bedrag"])]

    controles: list[dict] = []
    schoon = re.sub(r"\s+", " ", tekst)
    letterlijk = [(f"omzet {o.groep} {o.tarief}%", o.bruto) for o in omzet if not o.afgeleid and o.bruto]
    letterlijk += [(f"betaling {b['betaalwijze']}", _d(b["bedrag"])) for b in r["betalingen"]
                   if _d(b["bedrag"]) and not b["afgeleid"]]
    letterlijk += [(f"btw-overzicht {t['tarief']}%", _d(t["incl_btw"])) for t in r["rapport_totalen"]
                   if _d(t["incl_btw"]) and not t["afgeleid"]]
    letterlijk += [(naam, _d(r[k])) for naam, k in (("fooi", "fooi"), ("overige inkomsten", "overige_inkomsten"))
                   if _d(r[k])]
    weg = [f"{wat} {euro_nl(x)}" for wat, x in letterlijk if not staat_in(x, schoon)]
    controle(controles, "FOUT" if weg else "OK", "Bedragen staan in het rapport",
             ("niet gevonden in het rapport: " + "; ".join(weg)) if weg else
             f"alle {len(letterlijk)} overgenomen bedragen staan letterlijk in het rapport")
    afgeleid = [f"omzet {o.groep} {o.tarief}% {euro_nl(o.bruto)}" for o in omzet if o.afgeleid]
    afgeleid += [f"betaling {b['betaalwijze']} {euro_nl(_d(b['bedrag']))}" for b in r["betalingen"] if b["afgeleid"]]
    if afgeleid:
        controle(controles, "LET OP", "Door Claude uitgerekende bedragen", "; ".join(afgeleid))
    if any(t["afgeleid"] for t in r["rapport_totalen"]):
        controle(controles, "LET OP", "Btw-overzicht", "het rapport heeft geen btw-overzicht per tarief; Claude "
                 "heeft het uitgerekend, dus de omzetcontrole per tarief is niet onafhankelijk")
    return Kassaomzet(str(r["bedrijf"]).strip(), van, tot, f"Claude · {str(r['kassasysteem']).strip() or 'onbekend'}",
                      omzet, betalingen, _d(r["fooi"]), _d(r["overige_inkomsten"]), totalen, "btw-overzicht", niet,
                      controles, [str(o) for o in r["opmerkingen"]][:5], list(namen), (tot - van).days + 1, True)


def lees_met_claude(bestanden, ai) -> Kassaomzet:
    """`bestanden`: lijst (naam, bytes). `ai`: ClaudeAI. Kost één verzoek (± $0,04-0,10)."""
    bestanden = [(n, bytes(b)) for n, b in bestanden]
    tekst = tekst_van_bestanden(bestanden)
    antwoord = ai._vraag("kassarapport lezen", SYSTEM, tekst, SCHEMA, max_tokens=16000)
    return naar_kassaomzet(antwoord, tekst, [n for n, _ in bestanden])
