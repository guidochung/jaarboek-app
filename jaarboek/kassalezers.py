"""Vaste lezers voor bekende kassaformaten -> `Kassaomzet` (het tussenformaat van omzet.py).

Elk formaat wordt herkend aan de titel van het rapport, niet aan de bestandsnaam:
- Dag- en productrapport (Excel, Engels of Nederlands), bijv. Bar Bellini;
- 'RESTAURANT SUMMARY MONTH REPORT' + 'CATEGORY SUMMARY REPORT' (PDF), bijv. Black Pepper;
- 'Omzetlijst' (PDF), bijv. Kazoku;
- 'Boekhouding overzicht' (PDF), bijv. Ninja Sushi;
- booq 'Rapportage diensten op omzetcategorie' (PDF), bijv. Cafe Bica. Geeft geen btw-tarief per categorie:
  dat komt uit de koppeling (vast tarief per categorie, plus één restpost die past op het btw-overzicht).

Een lezer neemt bedragen letterlijk over en controleert zijn eigen werk (subtotalen, kolomtotalen). Lukt dat
niet, dan volgt een OmzetFout; een onbekend formaat geeft OnbekendFormaat (dan kan Claude het proberen).
"""
from __future__ import annotations

import io
import re
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .omzet import (NUL, Betaalpost, Kassaomzet, OmzetFout, OnbekendFormaat, OnvolledigeInvoer, Omzetpost, _norm,
                    _tarief_volgorde, bedrijfsnaam, controle, euro_centen, euro_nl, soort_betaling)

_MAAND_AFK = {"jan": 1, "feb": 2, "mar": 3, "mrt": 3, "apr": 4, "may": 5, "mei": 5, "jun": 6, "jul": 7,
              "aug": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12}


def _laatste_dag(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _nl(s: str) -> Decimal:
    """'1.234,56' of '0' -> Decimal."""
    try:
        return Decimal(str(s).replace("€", "").strip().replace(".", "").replace(",", "."))
    except InvalidOperation:
        raise OmzetFout(f"bedrag '{s}' niet herkend") from None


NL_BEDRAG = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"


# ==========================================================================
# Excel: dag- en productrapport (Engels of Nederlands)
# ==========================================================================
# Tegenkant uit het dagrapport: (soort, naam, omschrijving in de boeking, kolom in dagrapport)
BETALINGEN = [
    ("pin", "Pinbetalingen", "pinbetalingen", "total card"),
    ("contant", "Contant", "contant", "total cash"),
    ("op_rekening", "Op rekening", "op rekening", "total on invoice"),
    ("reserveringen", "Reserveringen", "reserveringen", "reservations checked-in today"),
    ("cadeaubonnen", "Cadeaubonnen", "cadeaubonnen", "gift cards sold"),
]
NODIG_DAG = [k for *_, k in BETALINGEN] + ["total tip", "total other income"]
# Kolommen die de boeking zouden veranderen maar niet in het voorbeeld zitten:
# staat hier een bedrag, dan komt dat in het verschil terecht en melden we het.
NIET_GEBOEKT = ["payment surcharge", "total pre-paid", "total external payments",
                "total invoices paid by external", "gift card payments", "cash expenses", "bad debt",
                "tips settled from cash", "total difference", "card difference", "cash difference"]
# Nederlandse export van hetzelfde kassasysteem -> Engelse kolomnaam
_NL_KOLOMMEN = {
    "dag": "day", "naam": "name", "totaal overige inkomsten": "total other income",
    "betalingstoeslag": "payment surcharge", "totaal kaart": "total card", "totaal contant": "total cash",
    "totaal aanbetaald": "total pre-paid", "totale fooi": "total tip",
    "vandaag ingecheckte reserveringen": "reservations checked-in today", "cadeaubonnen verkocht": "gift cards sold",
    "cadeaubon betalingen": "gift card payments", "totaal op factuur": "total on invoice",
    "totaal verschil": "total difference", "kaartverschil": "card difference", "kasstorting": "cash deposit",
    "fooien verrekend uit contant": "tips settled from cash", "contant uitgaven": "cash expenses",
    "oninbare vordering": "bad debt",
}


def _kolom(c) -> str:
    n = _norm(c)
    for nl, en in ((r"nettobedrag btw (\d+)%", "net amount vat {}%"), (r"belastingbedrag btw (\d+)%", "tax amount vat {}%"),
                   (r"bruto ?bedrag btw (\d+)%", "gross amount vat {}%"), (r"totaal incl\. btw (\d+)%", "total incl. vat {}%")):
        if m := re.fullmatch(nl, n):
            return en.format(m[1])
    return _NL_KOLOMMEN.get(n, n)


@dataclass
class Dagrapport:
    bedrijf: str
    start: date
    eind: date
    totalen: dict[str, Decimal]          # kolomnaam (Engels, genormaliseerd) -> som over alle dagen
    dagen: int
    naam: str = ""
    labels: dict[str, str] = field(default_factory=dict)     # kolomnaam -> naam zoals in het rapport


@dataclass
class Categorie:
    naam: str
    netto: dict[int, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    bruto: dict[int, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    producten: int = 0


@dataclass
class Productrapport:
    bedrijf: str
    start: date
    eind: date
    categorieen: list[Categorie]
    naam: str = ""


def _getal(w) -> bool:
    return isinstance(w, (int, float)) and not isinstance(w, bool)


def _dec(w) -> Decimal:
    return Decimal(str(w)) if _getal(w) else NUL


def _datum(w, naam: str) -> date:
    if isinstance(w, datetime):
        return w.date()
    if isinstance(w, date):
        return w
    s = str(w or "").strip()
    if m := re.match(r"([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s):          # Oct 1, 2026
        if m[1].lower() in _MAAND_AFK:
            return date(int(m[3]), _MAAND_AFK[m[1].lower()], int(m[2]))
    if m := re.match(r"(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\.?,?\s+(\d{4})", s):          # 1 okt. 2026 / 1 jul, 2026
        if m[2].lower() in _MAAND_AFK:
            return date(int(m[3]), _MAAND_AFK[m[2].lower()], int(m[1]))
    if m := re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := re.match(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s):
        return date(int(m[3]), int(m[2]), int(m[1]))
    raise OmzetFout(f"{naam}: datum '{s}' niet herkend.")


def _excel_rijen(bron, naam: str) -> list[tuple]:
    import openpyxl

    if isinstance(bron, (bytes, bytearray)):
        bron = io.BytesIO(bron)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")              # "Workbook contains no default style"
            wb = openpyxl.load_workbook(bron, data_only=True, read_only=True)
            rijen = [tuple(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
            wb.close()
    except Exception as e:                               # noqa: BLE001 - kapot of geen Excel
        raise OmzetFout(f"{naam}: kan dit Excel-bestand niet openen ({e}).") from None
    if not rijen or not rijen[0]:
        raise OmzetFout(f"{naam}: het bestand is leeg.")
    return rijen


DAG_TITELS, PRODUCT_TITELS = ("day report", "dagrapport"), ("product report", "productrapport", "product rapport")


def lees_kassarapport(bron, naam: str = "") -> Dagrapport | Productrapport:
    """Lees een dag- of productrapport (Excel). `bron` is een pad, bytes of een bestand."""
    naam = naam or (Path(bron).name if isinstance(bron, (str, Path)) else "rapport")
    rijen = _excel_rijen(bron, naam)
    titel = _norm(rijen[0][0])
    if titel not in DAG_TITELS + PRODUCT_TITELS:
        raise OnbekendFormaat(f"{naam}: geen dag- of productrapport (cel A1 is '{rijen[0][0]}').")
    bedrijf = str(rijen[0][1] or "").strip() if len(rijen[0]) > 1 else ""
    meta = {_norm(r[0]): r[1] for r in rijen[:12] if r and len(r) > 1 and r[0] is not None}
    start = next((meta[k] for k in ("start date", "startdatum", "begindatum") if k in meta), None)
    eind = next((meta[k] for k in ("end date", "einddatum") if k in meta), None)
    if start is None or eind is None:
        raise OmzetFout(f"{naam}: start- en einddatum niet gevonden bovenin het rapport.")
    start, eind = _datum(start, naam), _datum(eind, naam)
    if titel in DAG_TITELS:
        return _lees_dag(rijen, bedrijf, start, eind, naam)
    return _lees_product(rijen, bedrijf, start, eind, naam)


def _lees_dag(rijen, bedrijf, start, eind, naam) -> Dagrapport:
    kop, labels, totalen, dagen = None, {}, defaultdict(Decimal), 0
    for r in rijen:
        if not r:
            continue
        if _kolom(r[0]) == "day":
            kop = [_kolom(c) for c in r]
            labels = {_kolom(c): " ".join(str(c).split()) for c in r if c}
            continue
        if kop and (_getal(r[0]) or isinstance(r[0], (date, datetime))):
            dagen += 1
            for k, w in zip(kop, r):
                if k and _getal(w):
                    totalen[k] += _dec(w)
    if not kop or not dagen:
        raise OmzetFout(f"{naam}: geen dagregels gevonden in het dagrapport.")
    mist = [k for k in NODIG_DAG if k not in kop]
    if mist:
        raise OmzetFout(f"{naam}: kolom(men) ontbreken in het dagrapport: {', '.join(mist)}.")
    return Dagrapport(bedrijf, start, eind, dict(totalen), dagen, naam, labels)


def _lees_product(rijen, bedrijf, start, eind, naam) -> Productrapport:
    cats: list[Categorie] = []
    titel, kolommen, cat = None, None, None
    for r in rijen[1:]:
        if not r or r[0] is None or str(r[0]).strip() == "":
            continue
        eerste = _kolom(r[0])
        if eerste == "name":
            kolommen = {}
            for i, c in enumerate(r):
                if m := re.fullmatch(r"(net|gross) amount vat (\d+)%", _kolom(c)):
                    kolommen[i] = (m[1], int(m[2]))
            if not kolommen:
                raise OmzetFout(f"{naam}: kolommen 'Net/Gross Amount VAT' niet gevonden.")
            cat = next((c for c in cats if c.naam == titel), None)
            if cat is None:
                cat = Categorie(str(titel or "Zonder categorie").strip())
                cats.append(cat)
            continue
        if all(c is None or str(c).strip() == "" for c in r[1:]):
            titel, cat = r[0], None                      # kop van een nieuwe categorie
            continue
        if eerste in ("total", "grand total", "totaal"):
            cat = None
            continue
        if cat is not None:
            cat.producten += 1
            netto = defaultdict(Decimal)
            for i, (soort, t) in kolommen.items():
                w = _dec(r[i]) if i < len(r) else NUL
                (cat.bruto if soort == "gross" else netto)[t] += w
            for t, w in netto.items():
                cat.netto[t] += w
                if not any(s == "gross" and tt == t for s, tt in kolommen.values()):
                    cat.bruto[t] += w                    # tarief zonder bruto-kolom (0%)
    if not cats:
        raise OmzetFout(f"{naam}: geen categorieën gevonden in het productrapport.")
    return Productrapport(bedrijf, start, eind, cats, naam)


def uit_dag_en_productrapport(dag: Dagrapport, prod: Productrapport) -> Kassaomzet:
    """Omzet per categorie uit het productrapport; betalingen, fooi en btw-overzicht uit het dagrapport."""
    omzet = []
    for cat in prod.categorieen:
        for t in sorted(set(cat.bruto) | set(cat.netto), key=_tarief_volgorde):
            bruto = euro_centen(cat.bruto[t])
            if bruto:
                omzet.append(Omzetpost(cat.naam, t, bruto, euro_centen(cat.bruto[t] - cat.netto[t])))
    T = dag.totalen
    betalingen = [Betaalpost(label, euro_centen(T.get(k, NUL)), soort, oms)
                  for soort, label, oms, k in BETALINGEN if euro_centen(T.get(k, NUL))]
    totalen = {}
    for k in T:
        if m := re.fullmatch(r"net amount vat (\d+)%", k):
            tax = T.get(f"tax amount vat {m[1]}%", NUL)
            totalen[int(m[1])] = (euro_centen(T[k] + tax), euro_centen(tax))
    niet = [(dag.labels.get(k, k), euro_centen(T[k])) for k in NIET_GEBOEKT if euro_centen(T.get(k, NUL))]

    controles: list[dict] = []
    b_dag, b_prod = bedrijfsnaam(dag.bedrijf), bedrijfsnaam(prod.bedrijf)
    zelfde = _norm(b_dag) == _norm(b_prod) and (dag.start, dag.eind) == (prod.start, prod.eind)
    controle(controles, "OK" if zelfde else "FOUT", "Rapporten horen bij elkaar",
             f"{b_dag}, {dag.start:%d-%m-%Y} t/m {dag.eind:%d-%m-%Y}" if zelfde else
             f"dagrapport: {dag.bedrijf} {dag.start:%d-%m-%Y} t/m {dag.eind:%d-%m-%Y}; productrapport: "
             f"{prod.bedrijf} {prod.start:%d-%m-%Y} t/m {prod.eind:%d-%m-%Y}")
    return Kassaomzet(b_dag or b_prod, dag.start, dag.eind, "Dag- en productrapport (Excel)", omzet, betalingen,
                      euro_centen(T.get("total tip", NUL)), euro_centen(T.get("total other income", NUL)), totalen,
                      "dagrapport", niet, controles, [], [dag.naam, prod.naam], dag.dagen)


# ==========================================================================
# PDF-hulpjes
# ==========================================================================
@dataclass
class Woord:
    tekst: str
    x: float
    y: float                         # bovenkant; pagina's lopen door (pagina * 10000 + top)
    vet: bool


def _pdf(data: bytes, naam: str) -> tuple[str, list[list[Woord]]]:
    """Tekst (zonder opmaak) en woorden per regel, alle pagina's achter elkaar."""
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            tekst, woorden = [], []
            for i, p in enumerate(pdf.pages):
                tekst.append(p.extract_text() or "")
                woorden += [Woord(w["text"], w["x0"], i * 10000 + w["top"], "bold" in w["fontname"].lower())
                            for w in p.extract_words(extra_attrs=["fontname"])]
    except Exception as e:                               # noqa: BLE001
        raise OmzetFout(f"{naam}: kan deze PDF niet openen ({e}).") from None
    regels: list[list[Woord]] = []
    for w in sorted(woorden, key=lambda w: (w.y, w.x)):
        if regels and abs(w.y - regels[-1][0].y) <= 2:
            regels[-1].append(w)
        else:
            regels.append([w])
    return "\n".join(tekst), [sorted(r, key=lambda w: w.x) for r in regels]


def _regel_tekst(r: list[Woord]) -> str:
    return " ".join(w.tekst for w in r)


# ==========================================================================
# PDF: 'RESTAURANT SUMMARY MONTH REPORT' + 'CATEGORY SUMMARY REPORT' (Black Pepper)
# ==========================================================================
def _summary_kop(tekst: str, naam: str) -> tuple[str, datetime, datetime]:
    m = re.search(r"from:\s*(\d{1,2})-(\d{1,2})-(\d{2,4})\s+(\d{1,2}):(\d{2}).*?to:\s*(\d{1,2})-(\d{1,2})-(\d{2,4})"
                  r"\s+(\d{1,2}):(\d{2})", tekst)
    if not m:
        raise OmzetFout(f"{naam}: periode ('from: … to: …') niet gevonden.")
    jaar = lambda j: int(j) + 2000 if len(j) == 2 else int(j)            # noqa: E731
    van = datetime(jaar(m[3]), int(m[2]), int(m[1]), int(m[4]), int(m[5]))
    tot = datetime(jaar(m[8]), int(m[7]), int(m[6]), int(m[9]), int(m[10]))
    regels = [r.strip() for r in tekst.splitlines()]
    i = next((i for i, r in enumerate(regels) if r.lower().startswith("report generation time")), None)
    bedrijf = next((r for r in regels[i + 1:] if r), "") if i is not None else ""
    return bedrijf, van, tot


def _summary_periode(van: datetime, tot: datetime) -> tuple[date, date, str]:
    """Kassadag van 12:00 tot 12:00: 1-9 12:00 t/m 1-10 12:00 is september."""
    eind = tot.date() - timedelta(days=1) if tot.date() > van.date() and (tot.hour, tot.minute) <= (van.hour, van.minute) \
        else tot.date()
    noot = (f"Kassadag loopt van {van:%H:%M} tot {tot:%H:%M}; geboekt als {van:%d-%m-%Y} t/m {eind:%d-%m-%Y}."
            if (van.hour, van.minute) != (0, 0) else "")
    return van.date(), eind, noot


def _lees_summary_maand(naam: str, tekst: str, regels: list[list[Woord]]) -> dict:
    bedrijf, van, tot = _summary_kop(tekst, naam)
    totalen = {}
    for r in tekst.splitlines():
        if m := re.fullmatch(r"\s*(\d+)%\s+(-?\d+\.\d{2})\s+(-?\d+\.\d{2})\s+(-?\d+\.\d{2})\s*", r):
            totalen[int(m[1])] = (Decimal(m[4]), Decimal(m[3]))
    if not totalen:
        raise OmzetFout(f"{naam}: btw-overzicht (VAT RATE / NET REVENUE / VAT / TOTAL REVENUE) niet gevonden.")

    # Betalingen: groepen (vet, links) met daaronder de betaalwijzen (ingesprongen)
    kop = next((i for i, r in enumerate(regels) if [w.tekst for w in r[:2]] == ["TYPE", "AMOUNT"]), None)
    if kop is None:
        raise OmzetFout(f"{naam}: betalingen (PAYMENTS AND TIPS) niet gevonden.")
    links = regels[kop][0].x
    groepen, totaal = [], None
    for r in regels[kop + 1:]:
        getallen = [w for w in r if re.fullmatch(r"-?\d+\.\d{2}", w.tekst)]
        tekst_r = " ".join(w.tekst for w in r if w not in getallen)
        if len(getallen) != 3:
            break
        bedrag, fooi, som = (Decimal(w.tekst) for w in getallen)
        if tekst_r.upper() == "TOTAL":
            totaal = som
            break
        rij = {"naam": tekst_r, "bedrag": bedrag, "fooi": fooi, "som": som}
        if r[0].x <= links + 5:
            groepen.append({**rij, "details": []})
        elif groepen:
            groepen[-1]["details"].append(rij)
    if totaal is None or not groepen:
        raise OmzetFout(f"{naam}: betalingen (PAYMENTS AND TIPS) niet te lezen.")
    betalingen, fooi = [], NUL
    for g in groepen:
        if g["details"] and sum(d["som"] for d in g["details"]) != g["som"]:
            raise OmzetFout(f"{naam}: betaalwijzen onder '{g['naam']}' tellen niet op tot het subtotaal.")
        for d in g["details"] or [g]:
            betalingen.append(Betaalpost(d["naam"], d["som"], soort_betaling(d["naam"]) or soort_betaling(g["naam"])))
            fooi += d["fooi"]
    if sum(b.bedrag for b in betalingen) != totaal:
        raise OmzetFout(f"{naam}: betaalwijzen tellen niet op tot TOTAL ({totaal}).")
    return {"bedrijf": bedrijf, "van": van, "tot": tot, "totalen": totalen, "betalingen": betalingen, "fooi": fooi}


def _lees_summary_categorie(naam: str, tekst: str, regels: list[list[Woord]]) -> dict:
    bedrijf, van, tot = _summary_kop(tekst, naam)
    kop = next((i for i, r in enumerate(regels) if [w.tekst for w in r[:2]] == ["CATEGORY", "PRICE"]), None)
    if kop is None:
        raise OmzetFout(f"{naam}: kolommen CATEGORY / PRICE niet gevonden.")
    links, prijs = regels[kop][0].x, regels[kop][1].x
    # Categorieën staan links (x van 'CATEGORY'); producten springen in. Een categorie kan over twee regels lopen.
    stukken = [" ".join(w.tekst for w in r if w.x < prijs - 5) for r in regels[kop + 1:] if abs(r[0].x - links) <= 3]
    omzet = []
    for m in re.finditer(r"([^\[\]]+?)\s*\[([^\]]*)\]", " ".join(stukken)):
        groep = m[1].strip()
        for t, incl, btw in re.findall(r"(\d+)%:\s*(-?\d+\.\d{2})\s*\((-?\d+\.\d{2})\)", m[2]):
            omzet.append(Omzetpost(groep, int(t), Decimal(incl), Decimal(btw)))
    if not omzet:
        raise OmzetFout(f"{naam}: geen categorieën met omzet gevonden.")
    return {"bedrijf": bedrijf, "van": van, "tot": tot, "omzet": omzet}


def uit_summary_rapporten(maand: tuple[str, dict], categorie: tuple[str, dict] | None) -> Kassaomzet:
    (m_naam, m), controles, noten = maand, [], []
    start, eind, noot = _summary_periode(m["van"], m["tot"])
    noten += [noot] if noot else []
    if categorie:
        c_naam, c = categorie
        zelfde = _norm(c["bedrijf"]) == _norm(m["bedrijf"]) and (c["van"], c["tot"]) == (m["van"], m["tot"])
        controle(controles, "OK" if zelfde else "FOUT", "Rapporten horen bij elkaar",
                 f"{m['bedrijf']}, {m['van']:%d-%m-%Y %H:%M} t/m {m['tot']:%d-%m-%Y %H:%M}" if zelfde else
                 f"maandrapport: {m['bedrijf']} {m['van']:%d-%m-%Y} t/m {m['tot']:%d-%m-%Y}; categorierapport: "
                 f"{c['bedrijf']} {c['van']:%d-%m-%Y} t/m {c['tot']:%d-%m-%Y}")
        omzet, bestanden = c["omzet"], [m_naam, c_naam]
    else:
        omzet = [Omzetpost(f"Omzet {t}%", t, incl, btw) for t, (incl, btw) in sorted(m["totalen"].items()) if incl]
        bestanden = [m_naam]
        noten.append("Geen categorierapport geüpload: de omzet is per btw-tarief geboekt.")
    return Kassaomzet(m["bedrijf"], start, eind, "Restaurant summary month + category report (PDF)", omzet,
                      m["betalingen"], m["fooi"], NUL, m["totalen"], "maandrapport", [], controles, noten, bestanden,
                      (eind - start).days + 1)


# ==========================================================================
# PDF: 'Omzetlijst' (Kazoku)
# ==========================================================================
def lees_omzetlijst(naam: str, tekst: str, regels: list[list[Woord]]) -> Kassaomzet:
    m = re.search(r"Omzetlijst periode\s+(\d{2})-(\d{2})-(\d{4})\s+t/m\s+(\d{2})-(\d{2})-(\d{4})", tekst)
    if not m:
        raise OmzetFout(f"{naam}: periode ('Omzetlijst periode … t/m …') niet gevonden.")
    start, eind = date(int(m[3]), int(m[2]), int(m[1])), date(int(m[6]), int(m[5]), int(m[4]))
    bedrijf = next((r.strip() for r in tekst.splitlines() if r.strip()), "")
    controles, noten = [], []

    # Btw-overzicht onderaan: 'BTW 9% netto btw bruto'. Dit is wat het kantoor boekt (per tarief).
    totalen, totaal = {}, None
    for r in tekst.splitlines():
        if t := re.fullmatch(r"\s*BTW\s+(\d+)\s*%\s+(\S+)\s+(\S+)\s+(\S+)\s*", r):
            totalen[int(t[1])] = (_nl(t[4]), _nl(t[3]))
        elif t := re.fullmatch(rf"\s*Totaal\s+({NL_BEDRAG})\s+({NL_BEDRAG})\s+({NL_BEDRAG})\s*", r):
            totaal = (_nl(t[3]), _nl(t[2]))
    if not totalen:
        raise OmzetFout(f"{naam}: btw-overzicht ('BTW 9% …') niet gevonden.")
    omzet = [Omzetpost(f"Omzet {t}%", t, incl, btw) for t, (incl, btw) in sorted(totalen.items(), key=lambda x: _tarief_volgorde(x[0])) if incl]
    som = sum(i for i, _ in totalen.values())
    if totaal is not None:
        controle(controles, "OK" if abs(som - totaal[0]) <= Decimal("0.05") else "FOUT", "Btw-overzicht telt op",
                 f"tarieven samen {euro_nl(som)}, totaal in het rapport {euro_nl(totaal[0])}")

    # Categorieën (alleen ter controle: twee categorieën hebben omzet in 9% én 21%)
    cats, bezig = {}, False
    for r in tekst.splitlines():
        if r.strip().startswith("Omzet per categorie"):
            bezig = "Kassa" not in r and "Webshop" not in r
            continue
        if bezig and r.strip().startswith("Totaal"):
            bezig = False
        elif bezig and (c := re.match(rf"\s*(.+?)\s+({NL_BEDRAG})\s+\(", r)) and not c[1].endswith("*"):
            cats[c[1]] = _nl(c[2])
    if cats:
        cat_som = sum(cats.values())
        verschil = som - cat_som
        controle(controles, "OK" if abs(verschil) <= Decimal("0.05") else "LET OP", "Categorieën tegen btw-overzicht",
                 f"{len(cats)} categorieën samen {euro_nl(cat_som)}, btw-overzicht {euro_nl(som)}"
                 + (f"; het btw-overzicht telt {euro_nl(verschil)} meer mee. Geboekt is het btw-overzicht."
                    if abs(verschil) > Decimal("0.05") else ""))

    # Betalingen: eerste blok onder 'Datum / Aantal / Omzet / Online / Contant / Pin'. Elke cel heeft meerdere
    # regels (totaal, waarvan UE, fooi); per kolom is het bovenste bedrag zonder 'UE:'/'Fooi' het totaal.
    kop_i = next((i for i, r in enumerate(regels) if r and r[0].tekst == "Datum" and any(w.tekst == "Omzet" for w in r)), None)
    if kop_i is None:
        raise OmzetFout(f"{naam}: tabel met betalingen ('Datum … Omzet … Pin') niet gevonden.")
    kop = regels[kop_i]
    kolommen = []                                            # (naam, x-midden)
    for j, w in enumerate(kop):
        if j + 1 < len(kop) and kop[j + 1].tekst.startswith("(€") and w.tekst not in ("Aantal",):
            kolommen.append((w.tekst, (w.x + kop[j + 1].x) / 2 + 10))
    if not kolommen or kolommen[0][0] != "Omzet":
        raise OmzetFout(f"{naam}: kolommen van de betalingen niet herkend.")
    eerste, ue, fooi = {}, defaultdict(Decimal), defaultdict(Decimal)
    for r in regels[kop_i + 1:]:
        for j, w in enumerate(r):
            if not re.fullmatch(NL_BEDRAG, w.tekst):
                continue
            kol = min(kolommen, key=lambda k: abs(k[1] - (w.x + 20)))[0]
            voor = r[j - 1].tekst.rstrip(":").lower() if j else ""
            if j >= 2 and r[j - 1].tekst == ":":
                voor = r[j - 2].tekst.lower()
            if voor == "ue":
                ue[kol] += _nl(w.tekst)
            elif voor == "fooi":
                fooi[kol] += _nl(w.tekst)
            else:
                eerste.setdefault(kol, _nl(w.tekst))
        if "Omzet" in fooi:
            break                                            # fooiregel van de omzetkolom: einde eerste blok
    namen = [k for k, _ in kolommen[1:]]
    betalingen = [Betaalpost(k, eerste.get(k, NUL), soort_betaling(k)) for k in namen if eerste.get(k)]
    if "Omzet" not in eerste or sum(b.bedrag for b in betalingen) != eerste["Omzet"]:
        raise OmzetFout(f"{naam}: betalingen ({', '.join(namen)}) tellen niet op tot de omzet in het rapport.")
    if ue.get("Omzet"):
        noten.append(f"Waarvan Uber Eats {euro_nl(ue['Omzet'])}: " +
                     ", ".join(f"{k.lower()} {euro_nl(ue[k])}" for k in namen if ue.get(k)) + ".")
    niet = [("Fooi (staat los van de betalingen)", fooi["Omzet"])] if fooi.get("Omzet") else []
    if (k := re.search(rf"Korting\*\s+({NL_BEDRAG})", tekst)) and _nl(k[1]):
        noten.append(f"Korting {euro_nl(_nl(k[1]))} is volgens het rapport geen onderdeel van de omzet.")
    return Kassaomzet(bedrijf, start, eind, "Omzetlijst (PDF)", omzet, betalingen, NUL, NUL, totalen, "btw-overzicht",
                      niet, controles, noten, [naam], (eind - start).days + 1)


# ==========================================================================
# PDF: 'Boekhouding overzicht' (Ninja Sushi)
# ==========================================================================
def lees_boekhouding_overzicht(naam: str, tekst: str) -> Kassaomzet:
    m = re.search(r"Bestelperiode:\s*(\d{2})-(\d{2})-(\d{4})\s+t/m\s+(\d{2})-(\d{2})-(\d{4})", tekst)
    if not m:
        raise OmzetFout(f"{naam}: bestelperiode niet gevonden.")
    start, eind = date(int(m[3]), int(m[2]), int(m[1])), date(int(m[6]), int(m[5]), int(m[4]))
    b = re.search(r"Restaurant:\s*(.+)", tekst)
    bedrijf = b[1].strip() if b else ""
    sectie, kolommen, kanalen, totaal = "", [], {}, None
    totalen, btw_totaal, niet, controles, noten = {}, None, [], [], []
    for r in (x.strip() for x in tekst.splitlines()):
        if r in ("Bestellingen", "Overig", "BTW en omzet"):
            sectie = r
            continue
        if r.startswith("Aantal "):
            kolommen = re.findall(r"Op rekening|[^\s]+", r[len("Aantal "):])
            continue
        if sectie == "BTW en omzet" and (t := re.search(rf"\((\d+)%\)\s*:\s*€\s*({NL_BEDRAG})\s+€\s*({NL_BEDRAG})"
                                                       rf"\s+€\s*({NL_BEDRAG})", r)):
            totalen[int(t[1])] = (_nl(t[4]), _nl(t[3]))
            continue
        rij = re.fullmatch(rf"(.+?):\s+(\d+)\s+((?:€\s*{NL_BEDRAG}\s*)+)", r)
        if sectie == "BTW en omzet" and (t := re.fullmatch(rf"Totaal:\s+(?:€\s*{NL_BEDRAG}\s+){{2}}€\s*({NL_BEDRAG})", r)):
            btw_totaal = _nl(t[1])
        if not rij or not kolommen:
            continue
        bedragen = [_nl(x) for x in re.findall(NL_BEDRAG, rij[3])]
        if len(bedragen) != len(kolommen):
            raise OmzetFout(f"{naam}: regel '{rij[1]}' heeft {len(bedragen)} bedragen, verwacht {len(kolommen)}.")
        waarden = dict(zip(kolommen, bedragen))
        if sectie == "Bestellingen" and rij[1] == "Totaal":
            totaal = waarden
        elif sectie == "Bestellingen" and rij[1] != "Geannuleerd":
            kanalen[rij[1]] = waarden.get("Totaal", NUL)
        elif sectie == "Overig" and waarden.get("Totaal"):
            niet.append((rij[1], waarden["Totaal"]))
    if totaal is None or not totalen:
        raise OmzetFout(f"{naam}: totaal van de bestellingen of het btw-overzicht niet gevonden.")
    betalingen = [Betaalpost(k, v, soort_betaling(k)) for k, v in totaal.items() if k != "Totaal" and v]
    if sum(b.bedrag for b in betalingen) != totaal.get("Totaal"):
        raise OmzetFout(f"{naam}: betaalwijzen tellen niet op tot het totaal van de bestellingen.")
    omzet = [Omzetpost(f"Omzet {t}%", t, incl, btw) for t, (incl, btw) in sorted(totalen.items(), key=lambda x: _tarief_volgorde(x[0])) if incl]
    som = sum(i for i, _ in totalen.values())
    if btw_totaal is not None:
        controle(controles, "OK" if abs(som - btw_totaal) <= Decimal("0.05") else "FOUT", "Btw-overzicht telt op",
                 f"tarieven samen {euro_nl(som)}, totaal in het rapport {euro_nl(btw_totaal)}")
    if kanalen:
        noten.append("Bestellingen per kanaal: " + ", ".join(f"{k} {euro_nl(v)}" for k, v in kanalen.items() if v) + ".")
    return Kassaomzet(bedrijf, start, eind, "Boekhouding overzicht (PDF)", omzet, betalingen, NUL, NUL, totalen,
                      "btw-overzicht", niet, controles, noten, [naam], (eind - start).days + 1)


# ==========================================================================
# PDF: booq 'Rapportage diensten op omzetcategorie' (Cafe Bica)
# ==========================================================================
def lees_booq(naam: str, tekst: str, regels: list[list[Woord]]) -> Kassaomzet:
    """Omzet per subcategorie (ingesprongen; hoofdgroepen staan links en tellen niet mee), zonder btw-tarief.

    Het tarief per categorie komt uit de koppeling; het btw-overzicht (basisbedrag + btw) is de controle.
    """
    kop = re.search(r"Rapportage diensten op omzetcategorie\s*(.*)", tekst)
    bedrijf = kop[1].strip() if kop else ""
    van = re.search(r"Datum van:\s*(\d{2})-(\d{2})-(\d{4})", tekst)
    tot = re.search(r"Datum t/m:\s*(\d{2})-(\d{2})-(\d{4})", tekst)
    if not van or not tot:
        raise OmzetFout(f"{naam}: periode ('Datum van' / 'Datum t/m') niet gevonden.")
    start, eind = date(int(van[3]), int(van[2]), int(van[1])), date(int(tot[3]), int(tot[2]), int(tot[1]))

    # Categorieën: naam op een ingesprongen regel, IdNr/aantal/bedrag op de regel eronder
    i = next((i for i, r in enumerate(regels) if _regel_tekst(r).startswith("Totalen verkopen")), None)
    if i is None:
        raise OmzetFout(f"{naam}: 'Totalen verkopen' niet gevonden.")
    links = regels[i][0].x
    omzet, totaal, naam_cat = [], None, None
    for r in regels[i + 1:]:
        tekst_r = _regel_tekst(r)
        bedrag = next((w.tekst for w in r if re.fullmatch(r"-?\d+,\d{2}", w.tekst)), None)
        if r[0].x <= links + 5:                              # hoofdgroep of totaal
            naam_cat = None
            if tekst_r.startswith("Totaal"):
                totaal = _nl(bedrag) if bedrag else None
                break
        elif bedrag is None:
            naam_cat = tekst_r                               # naam van een subcategorie
        elif naam_cat is not None:
            if _nl(bedrag):
                omzet.append(Omzetpost(naam_cat, None, _nl(bedrag)))
            naam_cat = None
    if totaal is None or sum(o.bruto for o in omzet) != totaal:
        raise OmzetFout(f"{naam}: de omzetcategorieën tellen niet op tot het totaal van het rapport.")

    totalen = {}
    for m in re.finditer(r"BTW\s+(\d+)(?:[.,]\d+)?\s*%\s+(-?\d+,\d{2})\s+(-?\d+,\d{2})", tekst):
        basis, btw = _nl(m[2]), _nl(m[3])
        totalen[int(m[1])] = (basis + btw, btw)
    if not totalen:
        raise OmzetFout(f"{naam}: btw-overzicht niet gevonden.")
    controles, noten = [], []
    som = sum(i for i, _ in totalen.values())
    controle(controles, "OK" if som == totaal else "FOUT", "Btw-overzicht telt op",
             f"basisbedrag + btw per tarief samen {euro_nl(som)}, omzet volgens het rapport {euro_nl(totaal)}")

    # Betalingen ('naam bedrag', laagste niveau; 'Totaal' tussendoor is een paginatotaal) en overige posten
    sectie, betalingen, eindtotaal, niet = "", [], None, []
    for r in (x.strip() for x in tekst.splitlines()):
        if r.startswith("Totaal betalingen"):
            sectie = "betalingen"
        elif r.startswith("Totalen fooi"):
            sectie = "fooi"
        elif r.startswith("Verplichtingen"):
            sectie = "verplichtingen"
        elif r.startswith("Totalen statiegeld"):
            sectie = "statiegeld"
        elif r.startswith("Totalen kassa transacties"):
            sectie = "kastransacties"
        elif r.startswith("Totalen"):
            sectie = ""
        elif sectie == "betalingen" and (m := re.fullmatch(r"(.+?)\s+(-?\d+\.\d{2})", r)):
            if m[1] == "Totaal":
                eindtotaal = Decimal(m[2])
            elif Decimal(m[2]):
                betalingen.append(Betaalpost(m[1], Decimal(m[2]), soort_betaling(m[1])))
        elif sectie and sectie != "betalingen" and (m := re.fullmatch(r"(.+?)\s+(?:\d+\s+)?(-?\d+,\d{2})", r)):
            if m[1] == "Totaal" and sectie in ("fooi", "statiegeld", "kastransacties") and _nl(m[2]):
                niet.append(({"fooi": "Fooi", "statiegeld": "Statiegeld", "kastransacties": "Kastransacties"}[sectie],
                             _nl(m[2])))
            elif m[1] != "Totaal" and sectie == "verplichtingen" and _nl(m[2]):
                niet.append((m[1], _nl(m[2])))
    if eindtotaal is None or sum(b.bedrag for b in betalingen) != eindtotaal:
        raise OmzetFout(f"{naam}: de betaalwijzen tellen niet op tot het totaal van de betalingen.")
    noten.append("Booq geeft geen btw-tarief per categorie: het tarief komt uit de koppeling; één restpost vangt "
                 "het verschil met het btw-overzicht op.")
    return Kassaomzet(bedrijf, start, eind, "booq · Rapportage diensten op omzetcategorie (PDF)", omzet, betalingen,
                      NUL, NUL, totalen, "btw-overzicht", niet, controles, noten, [naam], (eind - start).days + 1)


# ==========================================================================
# Herkennen en samenvoegen
# ==========================================================================
def _herken(naam: str, data: bytes):
    """-> (soort, inhoud). Soort: dag, product, summary_maand, summary_categorie, omzetlijst, boekhouding, booq."""
    if data[:2] == b"PK":
        r = lees_kassarapport(data, naam)                # OnbekendFormaat als A1 geen bekende titel is
        return ("dag" if isinstance(r, Dagrapport) else "product"), r
    if data[:5] == b"%PDF-":
        tekst, regels = _pdf(data, naam)
        kop = tekst[:3000]
        if "SUMMARY MONTH REPORT" in kop:
            return "summary_maand", _lees_summary_maand(naam, tekst, regels)
        if "CATEGORY SUMMARY REPORT" in kop:
            return "summary_categorie", _lees_summary_categorie(naam, tekst, regels)
        if "Omzetlijst periode" in kop:
            return "omzetlijst", lees_omzetlijst(naam, tekst, regels)
        if "Boekhouding overzicht" in kop and "Bestelperiode" in kop:
            return "boekhouding", lees_boekhouding_overzicht(naam, tekst)
        if "Rapportage diensten op omzetcategorie" in kop:
            return "booq", lees_booq(naam, tekst, regels)
        raise OnbekendFormaat(f"{naam}: dit kassarapport (PDF) ken ik nog niet.")
    raise OnbekendFormaat(f"{naam}: dit bestandsformaat ken ik nog niet (geen Excel of PDF).")


def lees_omzet(bestanden) -> Kassaomzet:
    """`bestanden`: lijst (naam, pad-of-bytes) van één klant en één maand. Volgorde maakt niet uit."""
    gelezen = defaultdict(list)
    for naam, bron in bestanden:
        data = bytes(bron) if isinstance(bron, (bytes, bytearray)) else Path(bron).read_bytes()
        soort, inhoud = _herken(naam, data)
        gelezen[soort].append((naam, inhoud))
    soorten = set(gelezen)
    if not soorten:
        raise OnvolledigeInvoer("de omzetrapporten uit de kassa (Excel of PDF).")
    for soort, lijst in gelezen.items():
        if len(lijst) > 1:
            raise OmzetFout("Twee rapporten van dezelfde soort: " + ", ".join(n for n, _ in lijst) +
                            ". Upload per soort één rapport.")
    if soorten <= {"dag", "product"}:
        if soorten == {"dag", "product"}:
            return uit_dag_en_productrapport(gelezen["dag"][0][1], gelezen["product"][0][1])
        raise OnvolledigeInvoer("het productrapport." if "dag" in soorten else "het dagrapport.")
    if soorten <= {"summary_maand", "summary_categorie"}:
        if "summary_maand" not in soorten:
            raise OnvolledigeInvoer("het maandrapport (RESTAURANT SUMMARY MONTH REPORT).")
        cat = gelezen["summary_categorie"][0] if "summary_categorie" in soorten else None
        return uit_summary_rapporten(gelezen["summary_maand"][0], cat)
    if soorten == {"omzetlijst"}:
        return gelezen["omzetlijst"][0][1]
    if soorten == {"boekhouding"}:
        return gelezen["boekhouding"][0][1]
    if soorten == {"booq"}:
        return gelezen["booq"][0][1]
    raise OmzetFout("De bestanden komen uit verschillende kassasystemen: " +
                    ", ".join(n for lijst in gelezen.values() for n, _ in lijst) + ".")
