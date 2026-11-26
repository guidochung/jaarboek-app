"""Kassa-omzet van een maand -> één kasboekboeking voor Asperion (FMUTAC-CSV).

Invoer: het dagrapport (betaalwijzen, fooi, overige inkomsten) en het productrapport
(omzet per kassacategorie en btw-tarief) uit het kassasysteem, beide als Excel.

Per kassacategorie en btw-tarief komt er één omzetregel: bedrag incl. btw, btw-code en
btw-bedrag, op de grootboekrekening uit de koppeltabel. De tegenkant (pin, contant, op
rekening, reserveringen, cadeaubonnen) en de fooi komen uit het dagrapport. Wat dan nog
overblijft om de boeking op nul te laten sluiten, wordt de regel 'verschil'.

Alles is gewone rekenkunde: geen AI. Plus- en mintekens en kolommen zijn precies zoals in
de boeking die het kantoor in Asperion inleest (zie tests/test_omzet.py).
"""
from __future__ import annotations

import copy
import io
import json
import re
import unicodedata
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import BASIS
from .structuur import MAANDEN

STANDAARD_KOPPELING = BASIS / "omzet_koppeling.json"
CENT = Decimal("0.01")
NUL = Decimal("0")
MARGE = Decimal("0.05")           # afrondingsruimte tussen product- en dagrapport
AANTAL_KOLOMMEN = 40              # FMUTAC-regel: 40 velden, gescheiden door ';'

# Tegenkant uit het dagrapport: (sleutel tegenrekening, omschrijving, kolom in dagrapport)
BETALINGEN = [
    ("pin", "pinbetalingen", "total card"),
    ("contant", "contant", "total cash"),
    ("op_rekening", "op rekening", "total on invoice"),
    ("reserveringen", "reserveringen", "reservations checked-in today"),
    ("cadeaubonnen", "cadeaubonnen", "gift cards sold"),
]
TEGENREKENING_LABELS = {
    "pin": "Pinbetalingen", "contant": "Contant", "op_rekening": "Op rekening",
    "reserveringen": "Reserveringen", "cadeaubonnen": "Cadeaubonnen", "fooi": "Fooi",
    "overige_inkomsten": "Overige inkomsten", "verschil": "Verschil",
}
NODIG_DAG = [k for _, _, k in BETALINGEN] + ["total tip", "total other income"]
# Kolommen die de boeking zouden veranderen maar niet in het voorbeeld zitten:
# staat hier een bedrag, dan komt dat in het verschil terecht en melden we het.
NIET_GEBOEKT = ["payment surcharge", "total pre-paid", "total external payments",
                "total invoices paid by external", "gift card payments", "cash expenses", "bad debt",
                "tips settled from cash", "total difference", "card difference", "cash difference"]

_MAAND_AFK = {"jan": 1, "feb": 2, "mar": 3, "mrt": 3, "apr": 4, "may": 5, "mei": 5, "jun": 6, "jul": 7,
              "aug": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12}


class OmzetFout(Exception):
    """Invoer is niet te verwerken; de melding is bedoeld voor de gebruiker."""


# --------------------------------------------------------------------------
# Inlezen
# --------------------------------------------------------------------------
@dataclass
class Dagrapport:
    bedrijf: str
    start: date
    eind: date
    totalen: dict[str, Decimal]          # kolomnaam (genormaliseerd) -> som over alle dagen
    dagen: int
    naam: str = ""


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


def _norm(s) -> str:
    return " ".join(str(s).split()).casefold() if s is not None else ""


def _getal(w) -> bool:
    return isinstance(w, (int, float)) and not isinstance(w, bool)


def _dec(w) -> Decimal:
    return Decimal(str(w)) if _getal(w) else NUL


def euro_centen(x: Decimal) -> Decimal:
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def _datum(w, naam: str) -> date:
    if isinstance(w, datetime):
        return w.date()
    if isinstance(w, date):
        return w
    s = str(w or "").strip()
    if m := re.match(r"([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s):          # Oct 1, 2026
        if m[1].lower() in _MAAND_AFK:
            return date(int(m[3]), _MAAND_AFK[m[1].lower()], int(m[2]))
    if m := re.match(r"(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{4})", s):            # 1 okt. 2026
        if m[2].lower() in _MAAND_AFK:
            return date(int(m[3]), _MAAND_AFK[m[2].lower()], int(m[1]))
    if m := re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := re.match(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s):
        return date(int(m[3]), int(m[2]), int(m[1]))
    raise OmzetFout(f"{naam}: datum '{s}' niet herkend.")


def lees_kassarapport(bron, naam: str = "") -> Dagrapport | Productrapport:
    """Lees een dag- of productrapport (Excel). `bron` is een pad, bytes of een bestand."""
    import openpyxl

    naam = naam or (Path(bron).name if isinstance(bron, (str, Path)) else "rapport")
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

    titel = _norm(rijen[0][0])
    bedrijf = str(rijen[0][1] or "").strip() if len(rijen[0]) > 1 else ""
    meta = {_norm(r[0]): r[1] for r in rijen[:12] if r and len(r) > 1 and r[0] is not None}
    start = meta.get("start date", meta.get("startdatum"))
    eind = meta.get("end date", meta.get("einddatum"))
    if start is None or eind is None:
        raise OmzetFout(f"{naam}: start- en einddatum niet gevonden bovenin het rapport.")
    start, eind = _datum(start, naam), _datum(eind, naam)

    if titel in ("day report", "dagrapport"):
        return _lees_dag(rijen, bedrijf, start, eind, naam)
    if titel in ("product report", "productrapport"):
        return _lees_product(rijen, bedrijf, start, eind, naam)
    raise OmzetFout(f"{naam}: geen dag- of productrapport (cel A1 is '{rijen[0][0]}').")


def _lees_dag(rijen, bedrijf, start, eind, naam) -> Dagrapport:
    kop, totalen, dagen = None, defaultdict(Decimal), 0
    for r in rijen:
        if not r:
            continue
        if _norm(r[0]) in ("day", "dag"):
            kop = [_norm(c) for c in r]
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
    return Dagrapport(bedrijf, start, eind, dict(totalen), dagen, naam)


def _lees_product(rijen, bedrijf, start, eind, naam) -> Productrapport:
    cats: list[Categorie] = []
    titel, kolommen, cat = None, None, None
    for r in rijen[1:]:
        if not r or r[0] is None or str(r[0]).strip() == "":
            continue
        eerste = _norm(r[0])
        if eerste == "name":
            kolommen = {}
            for i, c in enumerate(r):
                if m := re.fullmatch(r"(net|gross) amount vat (\d+)%", _norm(c)):
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


def lees_kassarapporten(bronnen) -> tuple[Dagrapport, Productrapport]:
    """`bronnen`: lijst (naam, pad-of-bytes). Volgorde maakt niet uit."""
    dag, prod = [], []
    for naam, bron in bronnen:
        r = lees_kassarapport(bron, naam)
        (dag if isinstance(r, Dagrapport) else prod).append(r)
    if len(dag) != 1 or len(prod) != 1:
        delen = []
        if len(dag) != 1:
            delen.append(f"{len(dag)} dagrapport(en)")
        if len(prod) != 1:
            delen.append(f"{len(prod)} productrapport(en)")
        raise OmzetFout("Upload precies één dagrapport en één productrapport (nu: " + ", ".join(delen) + ").")
    return dag[0], prod[0]


# --------------------------------------------------------------------------
# Koppeling kassacategorie -> grootboek
# --------------------------------------------------------------------------
def laad_koppeling(pad: Path | None = None) -> dict:
    return json.loads(Path(pad or STANDAARD_KOPPELING).read_text(encoding="utf-8"))


def koppeling_uit_json(tekst: str | bytes) -> dict:
    """Een geüploade koppeling: ontbrekende onderdelen komen uit de standaard."""
    try:
        data = json.loads(tekst)
    except ValueError as e:
        raise OmzetFout(f"Koppeling is geen geldige JSON ({e}).") from None
    if not isinstance(data, dict) or not isinstance(data.get("categorieen", []), list):
        raise OmzetFout("Koppeling heeft niet de verwachte vorm (verwacht: 'categorieen' als lijst).")
    basis = laad_koppeling()
    for k, v in data.items():
        if k == "tegenrekeningen" and isinstance(v, dict):
            basis[k] = {**basis[k], **{kk: str(vv) for kk, vv in v.items()}}
        elif k == "btw_codes" and isinstance(v, dict):
            basis[k] = {**basis[k], **{str(kk): str(vv) for kk, vv in v.items()}}
        else:
            basis[k] = v
    for c in basis["categorieen"]:
        if not isinstance(c, dict) or not c.get("categorie"):
            raise OmzetFout("Elke categorie in de koppeling heeft minimaal een 'categorie' nodig.")
        c.setdefault("tarief", "")
        c.setdefault("omschrijving", c["categorie"])
        c.setdefault("rekening", "")
        c["tarief"], c["rekening"] = str(c["tarief"] or ""), str(c["rekening"] or "")
    return basis


@dataclass
class Koppelregel:
    categorie: str
    tarief: int
    omschrijving: str
    rekening: str
    bruto: Decimal                  # incl. btw, op centen
    btw: Decimal
    netto: Decimal
    bekend: bool                    # stond al in de koppeltabel
    gesplitst: bool                 # categorie heeft omzet in meer dan één tarief


def _tarief_volgorde(t: int):
    return (t == 0, t)              # 9, 21, ..., 0 achteraan


def stel_koppeling_voor(product: Productrapport, koppeling: dict) -> list[Koppelregel]:
    """Eén regel per kassacategorie en btw-tarief, met omschrijving en rekening uit de koppeling."""
    tabel = {(_norm(k["categorie"]), str(k.get("tarief") or "")): k for k in koppeling.get("categorieen", [])}
    uit = []
    for cat in product.categorieen:
        posten = []
        for t in sorted(set(cat.bruto) | set(cat.netto), key=_tarief_volgorde):
            bruto, netto = euro_centen(cat.bruto[t]), euro_centen(cat.netto[t])
            btw = euro_centen(cat.bruto[t] - cat.netto[t])
            if bruto or btw:
                posten.append((t, bruto, btw, netto))
        gesplitst = len(posten) > 1
        for t, bruto, btw, netto in posten:
            achter = f" {t}%" if gesplitst else ""
            if k := tabel.get((_norm(cat.naam), str(t))):
                oms, rek, bekend = k.get("omschrijving") or cat.naam + achter, k.get("rekening", ""), True
            elif k := tabel.get((_norm(cat.naam), "")):
                oms, rek, bekend = (k.get("omschrijving") or cat.naam) + achter, k.get("rekening", ""), True
            else:
                oms, rek, bekend = cat.naam + achter, "", False
            uit.append(Koppelregel(cat.naam, t, oms, str(rek or "").strip(), bruto, btw, netto, bekend, gesplitst))
    return uit


def bewaar_koppeling(koppeling: dict, regels: list[Koppelregel]) -> dict:
    """Nieuwe koppeling met de keuzes uit `regels` erin (de oude blijft ongewijzigd)."""
    nieuw = copy.deepcopy(koppeling)
    lijst = nieuw.setdefault("categorieen", [])
    for r in regels:
        if not r.rekening.strip():
            continue
        cat = _norm(r.categorie)
        spec = next((c for c in lijst if _norm(c["categorie"]) == cat and str(c.get("tarief") or "") == str(r.tarief)), None)
        alg = next((c for c in lijst if _norm(c["categorie"]) == cat and not str(c.get("tarief") or "")), None)
        if spec is not None:
            spec.update(omschrijving=r.omschrijving, rekening=r.rekening)
        elif alg is not None and not r.gesplitst:
            alg.update(omschrijving=r.omschrijving, rekening=r.rekening)
        elif alg is not None and alg.get("rekening") == r.rekening and \
                f"{alg.get('omschrijving') or r.categorie} {r.tarief}%" == r.omschrijving:
            continue                                     # volgt al uit de algemene regel
        else:
            lijst.append({"categorie": r.categorie, "tarief": str(r.tarief) if r.gesplitst else "",
                          "omschrijving": r.omschrijving, "rekening": r.rekening})
    return nieuw


# --------------------------------------------------------------------------
# Boeking
# --------------------------------------------------------------------------
@dataclass
class Boekregel:
    omschrijving: str
    rekening: str = ""
    bedrag: Decimal | None = None
    btw_code: str = ""
    btw_bedrag: Decimal | None = None
    soort: str = ""                 # kop, betaling, fooi, verschil, omzet, overig


@dataclass
class Boeking:
    bedrijf: str
    start: date
    eind: date
    boekstuk: str
    dagboek: str
    regels: list[Boekregel]
    controles: list[dict]
    omzet_bruto: Decimal
    omzet_btw: Decimal
    betalingen: Decimal
    fooi: Decimal
    overige: Decimal
    verschil: Decimal

    @property
    def periode(self) -> str:
        return f"{self.eind:%Y-%m}"

    @property
    def fout(self) -> bool:
        return any(c["status"] == "FOUT" for c in self.controles)

    @property
    def bestandsnaam(self) -> str:
        naam = re.sub(r"[^A-Za-z0-9]+", "_", _ascii(self.bedrijf)).strip("_") or "Omzet"
        return f"{naam}_Omzet_{self.periode}_Kasboek.csv"


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def prefix_uit_naam(bedrijf: str) -> str:
    """'Bar Bellini' -> 'BELL' (eerste woord dat geen 'bar', 'restaurant', 'bv' … is, 4 letters)."""
    woorden = re.findall(r"[A-Za-z]+", _ascii(bedrijf))
    overslaan = {"bar", "restaurant", "cafe", "eetcafe", "brasserie", "hotel", "de", "het", "the", "bv", "b",
                 "v", "nv", "vof", "holding"}
    kandidaten = [w for w in woorden if w.lower() not in overslaan] or woorden
    return (kandidaten[0][:4] if kandidaten else "OMZ").upper()


def euro_nl(x: Decimal | None) -> str:
    """Decimal -> '€ 1.234,56'."""
    if x is None:
        return ""
    s = f"{abs(x):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"€ {'-' if x < 0 else ''}{s}"


def maak_boeking(dag: Dagrapport, product: Productrapport, regels: list[Koppelregel], koppeling: dict) -> Boeking:
    teg = {**laad_koppeling()["tegenrekeningen"], **koppeling.get("tegenrekeningen", {})}
    btw_codes = {str(k): str(v) for k, v in koppeling.get("btw_codes", {}).items()}
    bedrijf = dag.bedrijf or product.bedrijf
    p = f"{dag.eind:%Y-%m}"
    prefix = str(koppeling.get("boekstuk_prefix") or "").strip() or prefix_uit_naam(bedrijf)
    boekstuk = f"{prefix}{dag.eind:%m%y}"
    T = dag.totalen
    controles: list[dict] = []

    def controle(status, naam, detail):
        controles.append({"status": status, "controle": naam, "detail": detail})

    br: list[Boekregel] = [Boekregel(f"Omzet {MAANDEN[dag.eind.month - 1]} {dag.eind.year} {bedrijf}", soort="kop")]
    betaald = NUL
    for sleutel, oms, kolom in BETALINGEN:
        bedrag = euro_centen(T.get(kolom, NUL))
        betaald += bedrag
        if bedrag:
            br.append(Boekregel(f"Omzet {p} - {oms}", teg[sleutel], -bedrag, soort="betaling"))
    fooi = euro_centen(T.get("total tip", NUL))
    overige = euro_centen(T.get("total other income", NUL))
    omzet = sum((r.bruto for r in regels), NUL)
    verschil = betaald - omzet - fooi - overige
    if fooi:
        br.append(Boekregel(f"Tip {p}", teg["fooi"], fooi, soort="fooi"))
    if verschil:
        br.append(Boekregel(f"Omzet {p} - verschil", teg["verschil"], verschil, soort="verschil"))
    for r in regels:
        code = btw_codes.get(str(r.tarief), "")
        br.append(Boekregel(f"Omzet {p} {r.omschrijving}", r.rekening.strip(), r.bruto, code,
                            r.btw if code else None, soort="omzet"))
    if overige:
        br.append(Boekregel(f"Overige inkomsten {p}", teg["overige_inkomsten"], overige, soort="overig"))

    # -- controles --
    zelfde = _norm(dag.bedrijf) == _norm(product.bedrijf) and (dag.start, dag.eind) == (product.start, product.eind)
    controle("OK" if zelfde else "FOUT", "Rapporten horen bij elkaar",
             f"{dag.bedrijf}, {dag.start:%d-%m-%Y} t/m {dag.eind:%d-%m-%Y}" if zelfde else
             f"dagrapport: {dag.bedrijf} {dag.start:%d-%m-%Y} t/m {dag.eind:%d-%m-%Y}; productrapport: "
             f"{product.bedrijf} {product.start:%d-%m-%Y} t/m {product.eind:%d-%m-%Y}")
    hele_maand = (dag.start.day == 1 and (dag.start.year, dag.start.month) == (dag.eind.year, dag.eind.month)
                  and (dag.eind + timedelta(days=1)).day == 1)
    controle("OK" if hele_maand else "LET OP", "Periode is één hele maand",
             f"{dag.dagen} dagen; boekingsdatum {dag.eind:%d-%m-%Y}" if hele_maand else
             f"rapport loopt van {dag.start:%d-%m-%Y} t/m {dag.eind:%d-%m-%Y}; geboekt op {dag.eind:%d-%m-%Y}, periode {p}")

    tarieven = sorted({r.tarief for r in regels} | {int(m[1]) for k in T if (m := re.fullmatch(r"net amount vat (\d+)%", k))},
                      key=_tarief_volgorde)
    for t in tarieven:
        prod_netto = sum((r.netto for r in regels if r.tarief == t), NUL)
        prod_btw = sum((r.btw for r in regels if r.tarief == t), NUL)
        dag_netto = euro_centen(T.get(f"net amount vat {t}%", NUL))
        dag_btw = euro_centen(T.get(f"tax amount vat {t}%", NUL))
        if not (prod_netto or dag_netto or prod_btw or dag_btw):
            continue
        ok = abs(prod_netto - dag_netto) <= MARGE and abs(prod_btw - dag_btw) <= MARGE
        controle("OK" if ok else "FOUT", f"Omzet {t}% gelijk aan dagrapport",
                 f"excl. btw {euro_nl(prod_netto)} (dagrapport {euro_nl(dag_netto)}), "
                 f"btw {euro_nl(prod_btw)} (dagrapport {euro_nl(dag_btw)})")

    zonder = [r for r in regels if not r.rekening.strip()]
    ongeldig = [r for r in regels if r.rekening.strip() and not re.fullmatch(r"\d+", r.rekening.strip())]
    if zonder or ongeldig:
        delen = [f"{r.categorie} {r.tarief}%: geen rekening" for r in zonder]
        delen += [f"{r.categorie} {r.tarief}%: '{r.rekening}' is geen rekeningnummer" for r in ongeldig]
        controle("FOUT", "Elke omzetcategorie heeft een grootboekrekening", "; ".join(delen))
    else:
        nieuw = [r.categorie for r in regels if not r.bekend]
        controle("OK", "Elke omzetcategorie heeft een grootboekrekening",
                 f"{len(regels)} omzetregels" + (f"; nieuw gekoppeld: {', '.join(dict.fromkeys(nieuw))}" if nieuw else ""))

    zonder_code = [r for r in regels if not btw_codes.get(str(r.tarief)) and r.btw]
    nul = [r for r in regels if r.tarief == 0]
    if zonder_code:
        controle("FOUT", "Btw-code per tarief",
                 "geen btw-code voor " + ", ".join(sorted({f"{r.tarief}%" for r in zonder_code})))
    elif nul:
        controle("LET OP", "Btw-code per tarief",
                 "0%-omzet zonder btw-code: " + ", ".join(f"{r.categorie} {euro_nl(r.bruto)}" for r in nul))

    leeg = [b.omschrijving for b in br[1:] if b.soort != "omzet" and not str(b.rekening).strip()]
    if leeg:
        controle("FOUT", "Tegenrekeningen ingevuld", "geen rekening bij: " + ", ".join(leeg))

    som = sum((b.bedrag for b in br if b.bedrag is not None), NUL)
    controle("OK" if som == 0 else "FOUT", "Boeking sluit op nul", f"som van alle regels {euro_nl(som)}")
    controle("OK" if not verschil else "LET OP", "Verschil",
             f"{euro_nl(verschil)} op {teg['verschil']}: betalingen {euro_nl(betaald)} min omzet incl. btw "
             f"{euro_nl(omzet)}, fooi {euro_nl(fooi)} en overige inkomsten {euro_nl(overige)}")
    los = [(k, euro_centen(T[k])) for k in NIET_GEBOEKT if euro_centen(T.get(k, NUL))]
    controle("OK" if not los else "LET OP", "Overige kolommen dagrapport",
             "geen bedragen in kolommen die niet geboekt worden" if not los else
             "niet apart geboekt (zit in het verschil): " + ", ".join(f"{k} {euro_nl(v)}" for k, v in los))

    return Boeking(bedrijf, dag.start, dag.eind, boekstuk, str(koppeling.get("dagboek") or "10").strip(), br,
                   controles, omzet, sum((r.btw for r in regels), NUL), betaald, fooi, overige, verschil)


def naar_fmutac(boeking: Boeking) -> bytes:
    """De boeking als FMUTAC-CSV voor de kasboekimport van Asperion (UTF-8 met BOM, ';', punt als decimaalteken)."""
    datum = f"{boeking.eind:%d%m%Y}"
    regels = []
    for nr, b in enumerate(boeking.regels):
        v = [""] * AANTAL_KOLOMMEN
        v[0], v[1], v[2], v[5] = str(nr), "K", boeking.dagboek, boeking.boekstuk
        v[6] = re.sub(r"[;\r\n]+", " ", b.omschrijving).strip()
        v[7], v[8], v[14] = datum, b.rekening, "EUR"
        if b.bedrag is not None:
            v[12] = f"{b.bedrag:.2f}"
        if b.btw_code:
            v[20] = b.btw_code
            v[21] = f"{b.btw_bedrag:.2f}" if b.btw_bedrag is not None else ""
        regels.append(";".join(v))
    return ("﻿" + "\n".join(regels) + "\n").encode("utf-8")
