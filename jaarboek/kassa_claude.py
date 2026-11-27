"""Vangnet voor onbekende kassaformaten: Claude leest het rapport, code controleert.

Claude krijgt de tekst van de bestanden (PDF met kolomopmaak, Excel als tabeltekst) en zet die om naar
het tussenformaat (`Kassaomzet`). Geboekt worden alleen omzet (credit) en betalingen (debet); wat de
betalingen meer of minder zijn dan de omzet (fooi, verkochte cadeaukaarten, statiegeld …) komt als
'verschil' op de verschilrekening (1255). Die posten worden wel getoond, zodat het verschil te verklaren is.

Harde controles, zodat een leesfout nooit ongemerkt in de boeking komt:
- elk geboekt bedrag moet letterlijk in het rapport staan, of als basisbedrag + btw (anders FOUT);
- de omzetregels moeten per tarief optellen tot het btw-overzicht van het rapport (in maak_boeking);
- betalingen min omzet = verschil (in maak_boeking).
Bedragen die Claude echt zelf heeft uitgerekend krijgen LET OP.
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
    "required": ["kassasysteem", "bedrijf", "periode_van", "periode_tot", "omzet", "betalingen", "rapport_totalen",
                 "niet_geboekt", "opmerkingen"],
    "properties": {
        "kassasysteem": {"type": "string"},
        "bedrijf": {"type": "string"},
        "periode_van": {"type": "string"}, "periode_tot": {"type": "string"},
        "omzet": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["groep", "tarief", "incl_btw", "btw", "afgeleid"],
            "properties": {"groep": {"type": "string"},
                           "tarief": {"anyOf": [{"type": "integer"}, {"type": "null"}]},   # null: niet in het rapport
                           "incl_btw": GETAL, "btw": GETAL, "afgeleid": {"type": "boolean"}}}},
        "betalingen": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["betaalwijze", "bedrag", "afgeleid"],
            "properties": {"betaalwijze": {"type": "string"}, "bedrag": GETAL, "afgeleid": {"type": "boolean"}}}},
        "rapport_totalen": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["tarief", "incl_btw", "btw", "afgeleid"],
            "properties": {"tarief": {"type": "integer"}, "incl_btw": GETAL, "btw": GETAL,
                           "afgeleid": {"type": "boolean"}}}},
        "niet_geboekt": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["omschrijving", "bedrag", "afgeleid"],
            "properties": {"omschrijving": {"type": "string"}, "bedrag": GETAL, "afgeleid": {"type": "boolean"}}}},
        "opmerkingen": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = """Je zet het maandrapport van een horeca-kassasysteem om naar gegevens voor één omzetboeking in de \
boekhouding. De boekhouder boekt alleen de omzet (credit) en de betalingen (debet); wat de betalingen meer of \
minder zijn dan de omzet, komt als verschil op een vaste rekening. Regels:
- omzet: één regel per OMZETCATEGORIE van het rapport (niet per product), op het laagste niveau: zijn \
categorieën gegroepeerd in hoofdgroepen die alleen hun subcategorieën optellen, neem dan de subcategorieën. Laat \
categorieën zonder omzet weg. Per btw-tarief een regel; een categorie met omzet in twee tarieven krijgt twee regels. \
Staat er niet bij welk btw-tarief een categorie heeft, geef dan per categorie één regel met tarief null, het \
bedrag uit het rapport als incl_btw en btw 0; de boekhouder koppelt het tarief zelf. Alleen als het rapport \
helemaal geen categorieën heeft: één regel per tarief met groep "Omzet <tarief>%" en de bedragen uit het \
btw-overzicht. incl_btw = bedrag inclusief btw, btw = btw-bedrag zoals in het rapport (0 als het er niet staat).
- Neem bedragen letterlijk over. Moet je een bedrag uitrekenen (optellen, basisbedrag + btw, splitsen), zet dan \
afgeleid=true. Verzin nooit bedragen.
- betalingen: elke betaalwijze met het ontvangen bedrag, op het laagste detailniveau, ook betalingen met \
cadeaukaarten of -bonnen. Geen subtotalen of paginatotalen. Gebruik de namen uit het rapport.
- niet_geboekt: alles wat geen omzet en geen betaling is maar wel relevant voor de boekhouder: fooi, verkochte \
of uitgegeven cadeaukaarten, statiegeld, overige inkomsten, kortingen, kastransacties. Elke post apart, met het \
bedrag zoals in het rapport; tel posten niet bij elkaar op.
- rapport_totalen: het btw-overzicht van het rapport per tarief (incl. btw en btw). Staat alleen het \
basisbedrag er, dan incl_btw = basisbedrag + btw (afgeleid=true). Geeft het rapport geen totalen per tarief, \
reken ze uit (afgeleid=true).
- periode_van/periode_tot: kalenderdatums JJJJ-MM-DD. Loopt de kassadag door tot de volgende dag (bijv. \
12:00 tot 12:00), dan is periode_tot de laatste dag van de maand.
- kassasysteem: de naam van het kassasysteem en de titel waaraan je dit soort rapport herkent.
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


def in_rapport(bedrag: Decimal, btw: Decimal, schoon: str) -> bool:
    """Staat het bedrag in het rapport: letterlijk, of als basisbedrag + btw (allebei letterlijk)?"""
    return staat_in(bedrag, schoon) or (bool(btw) and staat_in(bedrag - btw, schoon) and staat_in(btw, schoon))


def naar_kassaomzet(r: dict, tekst: str, namen: list[str]) -> Kassaomzet:
    van, tot = _iso(r["periode_van"], "begindatum"), _iso(r["periode_tot"], "einddatum")
    if van.day == 1 and tot.day == 1 and tot > van and (tot - van).days >= 27:
        tot -= timedelta(days=1)                         # 1-9 t/m 1-10 (kassadag tot de volgende dag) = september
    omzet = [Omzetpost(str(o["groep"]).strip() or f"Omzet {o['tarief']}%",
                       None if o["tarief"] is None else int(o["tarief"]), _d(o["incl_btw"]), _d(o["btw"]),
                       bool(o["afgeleid"])) for o in r["omzet"]]
    betalingen = [Betaalpost(str(b["betaalwijze"]).strip(), _d(b["bedrag"]), soort_betaling(b["betaalwijze"]))
                  for b in r["betalingen"] if _d(b["bedrag"])]
    totalen = {int(t["tarief"]): (_d(t["incl_btw"]), _d(t["btw"])) for t in r["rapport_totalen"]}
    niet = [(str(n["omschrijving"]), _d(n["bedrag"])) for n in r["niet_geboekt"] if _d(n["bedrag"])]

    # Geboekte bedragen (en het btw-overzicht waartegen gecontroleerd wordt) moeten in het rapport staan
    controles: list[dict] = []
    schoon = re.sub(r"\s+", " ", tekst)
    posten = [(f"omzet {o.groep}" + ("" if o.tarief is None else f" {o.tarief}%"), o.bruto, o.btw or NUL, o.afgeleid)
              for o in omzet if o.bruto]
    posten += [(f"btw-overzicht {t['tarief']}%", _d(t["incl_btw"]), _d(t["btw"]), bool(t["afgeleid"]))
               for t in r["rapport_totalen"] if _d(t["incl_btw"])]
    posten += [(f"betaling {b['betaalwijze']}", _d(b["bedrag"]), NUL, bool(b["afgeleid"]))
               for b in r["betalingen"] if _d(b["bedrag"])]
    gevonden = [(wat, x, afg, in_rapport(x, btw, schoon)) for wat, x, btw, afg in posten]
    weg = [f"{wat} {euro_nl(x)}" for wat, x, afg, ok in gevonden if not ok and not afg]
    zelf = [f"{wat} {euro_nl(x)}" for wat, x, afg, ok in gevonden if not ok and afg]
    controle(controles, "FOUT" if weg else "OK", "Bedragen staan in het rapport",
             ("niet gevonden in het rapport: " + "; ".join(weg)) if weg else
             f"alle {len(gevonden) - len(zelf)} overgenomen bedragen staan in het rapport (letterlijk of als "
             "basisbedrag + btw)")
    if zelf:
        controle(controles, "LET OP", "Door Claude uitgerekende bedragen", "staan niet als zodanig in het rapport: "
                 + "; ".join(zelf))
    los = [f"{n['omschrijving']} {euro_nl(_d(n['bedrag']))}" for n in r["niet_geboekt"]
           if _d(n["bedrag"]) and not n["afgeleid"] and not staat_in(_d(n["bedrag"]), schoon)]
    if los:
        controle(controles, "LET OP", "Toelichtende posten", "niet gevonden in het rapport (niet geboekt, wel ter "
                 "verklaring van het verschil): " + "; ".join(los))
    return Kassaomzet(str(r["bedrijf"]).strip(), van, tot, f"Claude · {str(r['kassasysteem']).strip() or 'onbekend'}",
                      omzet, betalingen, NUL, NUL, totalen, "btw-overzicht", niet, controles,
                      [str(o) for o in r["opmerkingen"]][:5], list(namen), (tot - van).days + 1, True)


def lees_met_claude(bestanden, ai) -> Kassaomzet:
    """`bestanden`: lijst (naam, bytes). `ai`: ClaudeAI. Kost één verzoek (± $0,04-0,10)."""
    bestanden = [(n, bytes(b)) for n, b in bestanden]
    tekst = tekst_van_bestanden(bestanden)
    antwoord = ai._vraag("kassarapport lezen", SYSTEM, tekst, SCHEMA, max_tokens=16000)
    return naar_kassaomzet(antwoord, tekst, [n for n, _ in bestanden])
