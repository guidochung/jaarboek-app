"""Kassa-omzet van een maand -> één kasboekboeking voor Asperion (FMUTAC-CSV).

Elk kassasysteem levert andere rapporten. Een lezer zet ze om naar één tussenformaat, `Kassaomzet`:
omzet per kassacategorie (of per btw-tarief) incl. btw, betalingen per betaalwijze, fooi, overige
inkomsten en het btw-overzicht van het rapport zelf. Vaste lezers voor bekende kassa's staan in
kassalezers.py; een onbekend formaat kan Claude lezen (kassa_claude.py). Vanaf het tussenformaat is
alles hetzelfde: koppeling naar grootboekrekeningen, boekingsregels, controles en de CSV.

Per omzetregel: bedrag incl. btw, btw-code en btw-bedrag. Het btw-bedrag is incl. × tarief / (100 + tarief)
op centen, zoals het kantoor en Asperion rekenen (niet de btw uit de kassa, die per bon afrondt). De
tegenkant zijn de betalingen en de fooi; wat overblijft om op nul te sluiten is de regel 'verschil'.

Twee boekingsstijlen (per klant in de koppeling):
- uitgebreid: kopregel, elke betaalwijze apart, fooi, verschil, omzetregels (zoals Bar Bellini);
- compact: omzetregels, daarna de tegenkant samengevoegd per tegenrekening; overal dezelfde omschrijving.

Alles is gewone rekenkunde. Plus- en mintekens en kolommen zijn precies zoals in de boeking die het
kantoor in Asperion inleest (zie tests/test_omzet.py).
"""
from __future__ import annotations

import copy
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import BASIS
from .structuur import MAANDEN

STANDAARD_KOPPELING = BASIS / "omzet_koppeling.json"
CENT = Decimal("0.01")
NUL = Decimal("0")
MARGE = Decimal("0.05")           # afrondingsruimte tussen omzetregels en het btw-overzicht van de kassa
AANTAL_KOLOMMEN = 40              # FMUTAC-regel: 40 velden, gescheiden door ';'
STIJLEN = {"uitgebreid": "Uitgebreid: elke betaalwijze apart, omschrijving per regel",
           "samengevat": "Samengevat: omschrijving per omzetregel, tegenkant per tegenrekening",
           "compact": "Compact: tegenkant per tegenrekening, overal dezelfde omschrijving"}
BEDRAGEN = {"incl": "Omzetregels incl. btw", "excl": "Omzetregels excl. btw (btw-bedrag apart)"}
# Naam van een samengevoegde tegenregel ('Pinbetalingen 2026-10'), naar de soort van de eerste betaling erop
SOORT_LABEL = {"pin": "Pinbetalingen", "contant": "Contant", "online": "Online betalingen", "op_rekening": "Op rekening",
               "reserveringen": "Reserveringen", "cadeaubonnen": "Cadeaubonnen", "": "Betalingen"}

# Standaardsoorten betalingen (tegenrekening uit 'tegenrekeningen' in de koppeling) en overige tegenposten
TEGENREKENING_LABELS = {
    "pin": "Pin / kaart", "contant": "Contant", "online": "Online / bezorgplatforms", "op_rekening": "Op rekening",
    "reserveringen": "Reserveringen", "cadeaubonnen": "Cadeaubonnen", "fooi": "Fooi",
    "overige_inkomsten": "Overige inkomsten", "verschil": "Verschil",
}
BETAALSOORTEN = ["pin", "contant", "online", "op_rekening", "reserveringen", "cadeaubonnen"]


class OmzetFout(Exception):
    """Invoer is niet te verwerken; de melding is bedoeld voor de gebruiker."""


class OnbekendFormaat(OmzetFout):
    """Geen vaste lezer voor dit kassaformaat (Claude kan het proberen)."""


class OnvolledigeInvoer(OmzetFout):
    """Het formaat is bekend, maar er ontbreekt nog een rapport. De melding zegt welk."""


# --------------------------------------------------------------------------
# Tussenformaat
# --------------------------------------------------------------------------
@dataclass
class Omzetpost:
    groep: str                      # kassacategorie, of 'Omzet 9%' als het rapport alleen tarieven geeft
    tarief: int | None              # None: het rapport geeft geen tarief (komt dan uit de koppeling)
    bruto: Decimal                  # incl. btw
    btw: Decimal | None = None      # btw volgens het rapport (alleen ter informatie)
    afgeleid: bool = False          # uitgerekend, staat niet letterlijk in het rapport


@dataclass
class Betaalpost:
    naam: str                       # zoals in het rapport, bijv. 'Maestro' of 'Contant'
    bedrag: Decimal
    soort: str = ""                 # standaardsoort (BETAALSOORTEN) of '' als onbekend
    omschrijving: str = ""          # tekst in de uitgebreide boekingsstijl, bijv. 'pinbetalingen'


@dataclass
class Kassaomzet:
    bedrijf: str
    start: date
    eind: date                      # laatste dag van de periode = boekingsdatum
    lezer: str                      # welk formaat, en hoe gelezen
    omzet: list[Omzetpost]
    betalingen: list[Betaalpost]
    fooi: Decimal = NUL             # fooi die in de betalingen zit (wordt apart geboekt)
    overige: Decimal = NUL          # overige inkomsten die in de betalingen zitten
    totalen: dict[int, tuple[Decimal, Decimal]] = field(default_factory=dict)   # btw-overzicht: tarief -> (incl., btw)
    totalen_bron: str = "btw-overzicht"
    niet_geboekt: list[tuple[str, Decimal]] = field(default_factory=list)       # posten die niet in de boeking komen
    controles: list[dict] = field(default_factory=list)                          # controles van de lezer zelf
    opmerkingen: list[str] = field(default_factory=list)
    bestanden: list[str] = field(default_factory=list)
    dagen: int = 0
    door_claude: bool = False


def _norm(s) -> str:
    return " ".join(str(s).split()).casefold() if s is not None else ""


def euro_centen(x: Decimal) -> Decimal:
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def btw_uit_bruto(bruto: Decimal, tarief: int) -> Decimal:
    return euro_centen(Decimal(bruto) * tarief / (100 + tarief)) if tarief else NUL


def controle(lijst: list, status: str, naam: str, detail: str) -> None:
    lijst.append({"status": status, "controle": naam, "detail": detail})


def soort_betaling(naam: str) -> str:
    """Raad de standaardsoort van een betaalwijze uit de naam in het rapport ('' als onbekend)."""
    n = _norm(naam)
    if re.search(r"contant|\bcash\b|\bkas\b", n):
        return "contant"
    if re.search(r"op rekening|on invoice|op factuur|\binvoice", n):
        return "op_rekening"
    if re.search(r"cadeau|gift", n):
        return "cadeaubonnen"
    if re.search(r"reserver", n):
        return "reserveringen"
    if re.search(r"\bpin\b|card|kaart|maestro|master|visa|amex|american express|v[ -]?pay|debit|bancontact|"
                 r"apple pay|google pay|payconiq|\bpsp\b", n):              # betaalwijze gaat voor kanaal
        return "pin"
    if re.search(r"online|ideal|web|thuisbezorgd|uber|deliveroo|e-?commerce|just eat", n):
        return "online"
    return ""


def bedrijfsnaam(naam: str) -> str:
    """'Bar Bellini, Bar Bellini' -> 'Bar Bellini' (dubbele delen weg)."""
    delen = [d.strip() for d in str(naam or "").split(",") if d.strip()]
    uniek = list(dict.fromkeys(delen))
    return ", ".join(uniek) if len(uniek) < len(delen) else str(naam or "").strip()


# --------------------------------------------------------------------------
# Koppeling kassacategorie / betaalwijze -> grootboek
# --------------------------------------------------------------------------
def laad_koppeling(pad: Path | None = None) -> dict:
    return json.loads(Path(pad or STANDAARD_KOPPELING).read_text(encoding="utf-8"))


def koppeling_uit_json(tekst: str | bytes) -> dict:
    """Een geüploade of bewaarde koppeling: ontbrekende onderdelen komen uit de standaard."""
    try:
        data = json.loads(tekst)
    except ValueError as e:
        raise OmzetFout(f"Koppeling is geen geldige JSON ({e}).") from None
    if not isinstance(data, dict) or not isinstance(data.get("categorieen", []), list) \
            or not isinstance(data.get("betaalwijzen", []), list):
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
        c["btw_tarief"] = str(c.get("btw_tarief") or "")              # vast tarief als de kassa er geen geeft
        c["restpost"] = bool(c.get("restpost"))                         # vangt het verschil met het btw-overzicht
    for b in basis.setdefault("betaalwijzen", []):
        if not isinstance(b, dict) or not b.get("betaalwijze"):
            raise OmzetFout("Elke betaalwijze in de koppeling heeft minimaal een 'betaalwijze' nodig.")
        b["rekening"] = str(b.get("rekening") or "")
    if basis.get("stijl", "uitgebreid") not in STIJLEN:
        raise OmzetFout(f"Onbekende boekingsstijl '{basis['stijl']}' (kies uit: {', '.join(STIJLEN)}).")
    if basis.get("bedragen", "incl") not in BEDRAGEN:
        raise OmzetFout(f"Onbekende waarde voor 'bedragen': '{basis['bedragen']}' (kies uit: incl, excl).")
    return basis


@dataclass
class Koppelregel:
    categorie: str
    tarief: int | None              # None: nog geen tarief gekozen (kassa zonder tarief per categorie)
    omschrijving: str
    rekening: str
    bruto: Decimal                  # incl. btw, op centen
    btw: Decimal                    # incl. × tarief / (100 + tarief)
    netto: Decimal
    bekend: bool                    # stond al in de koppeltabel
    gesplitst: bool                 # categorie heeft omzet in meer dan één tarief
    vast: bool = False              # tarief komt uit de koppeling (de kassa geeft er geen)
    rest: bool = False              # restpost: vangt het verschil met het btw-overzicht op


@dataclass
class Betaalregel:
    naam: str
    soort: str
    omschrijving: str
    bedrag: Decimal
    rekening: str
    herkomst: str                   # 'koppeling', 'standaard' of 'nieuw'


def _tarief_volgorde(t: int | None):
    return (t is None, t == 0, t or 0)          # 9, 21, ..., 0 achteraan


def stel_koppeling_voor(kassa: Kassaomzet, koppeling: dict) -> list[Koppelregel]:
    """Eén regel per kassacategorie en btw-tarief, met omschrijving en rekening uit de koppeling."""
    tabel = {(_norm(k["categorie"]), str(k.get("tarief") or "")): k for k in koppeling.get("categorieen", [])}
    posten: dict[tuple[str, int | None], list] = {}          # (groep, tarief) -> [naam, bruto]; volgorde blijft
    for o in kassa.omzet:
        sleutel = (_norm(o.groep), None if o.tarief is None else int(o.tarief))
        posten.setdefault(sleutel, [o.groep, NUL])[1] += Decimal(o.bruto)
    tarieven = defaultdict(set)
    for (groep, t), (_, bruto) in posten.items():
        if euro_centen(bruto):
            tarieven[groep].add(t)
    uit = []
    for (groep, t), (naam, bruto) in posten.items():
        bruto = euro_centen(bruto)
        if not bruto:
            continue
        if t is None:                                        # tarief en restpost uit de koppeling
            k = tabel.get((groep, ""))
            rest = bool(k and k.get("restpost"))
            vast_t = str(k.get("btw_tarief") or "").strip() if k else ""
            tv = int(vast_t) if vast_t.isdigit() and not rest else None
            btw = btw_uit_bruto(bruto, tv) if tv is not None else NUL
            uit.append(Koppelregel(naam, tv, (k.get("omschrijving") if k else "") or naam,
                                   str(k.get("rekening") or "").strip() if k else "", bruto, btw, bruto - btw,
                                   k is not None, False, vast=True, rest=rest))
            continue
        gesplitst = len(tarieven[groep]) > 1
        achter = f" {t}%" if gesplitst else ""
        if k := tabel.get((groep, str(t))):
            oms, rek, bekend = k.get("omschrijving") or naam + achter, k.get("rekening", ""), True
        elif k := tabel.get((groep, "")):
            oms, rek, bekend = (k.get("omschrijving") or naam) + achter, k.get("rekening", ""), True
        else:
            oms, rek, bekend = naam + achter, "", False
        btw = btw_uit_bruto(bruto, t)
        uit.append(Koppelregel(naam, t, oms, str(rek or "").strip(), bruto, btw, bruto - btw, bekend, gesplitst))
    return uit


def verdeel_rest(regels: list[Koppelregel], kassa: Kassaomzet) -> tuple[list[Koppelregel], list[dict]]:
    """Restpost verdelen over de tarieven: per tarief btw-overzicht min de categorieën met een vast tarief.

    Zo zijn omzet én btw per tarief gelijk aan het btw-overzicht van de kassa (zoals het kantoor boekt).
    -> (regels met de restpost per tarief, controles).
    """
    rest = [r for r in regels if r.rest]
    controles: list[dict] = []
    if not rest:
        return regels, controles
    if len(rest) > 1:
        controle(controles, "FOUT", "Restpost", "meer dan één restpost: " + ", ".join(r.categorie for r in rest))
        return regels, controles
    r0 = rest[0]
    if not kassa.totalen:
        controle(controles, "FOUT", "Restpost", "het rapport heeft geen btw-overzicht om de restpost op te verdelen")
        return regels, controles
    vaste = [r for r in regels if not r.rest]
    posten = []
    for t in sorted(kassa.totalen, key=_tarief_volgorde):
        incl, btw = (euro_centen(x) for x in kassa.totalen[t])
        ri = incl - sum((r.bruto for r in vaste if r.tarief == t), NUL)
        rb = btw - sum((r.btw for r in vaste if r.tarief == t), NUL)
        if ri or rb:
            posten.append((t, ri, rb))
    som = sum((ri for _, ri, _ in posten), NUL)
    negatief = [f"{t}% {euro_nl(ri)}" for t, ri, _ in posten if ri < 0]
    ok = abs(som - r0.bruto) <= MARGE and not negatief
    controle(controles, "OK" if ok else "FOUT", f"Restpost {r0.categorie}",
             f"{euro_nl(r0.bruto)} in het rapport, verdeeld: " + ", ".join(f"{t}% {euro_nl(ri)}" for t, ri, _ in posten)
             + ("" if ok else f" (samen {euro_nl(som)}" + (f"; negatief: {', '.join(negatief)}" if negatief else "")
                + "; controleer de vaste tarieven van de andere categorieën)"))
    gesplitst = len(posten) > 1
    nieuw = [Koppelregel(r0.categorie, t, r0.omschrijving + (f" {t}%" if gesplitst else ""), r0.rekening, ri, rb,
                         ri - rb, r0.bekend, gesplitst, vast=True, rest=True) for t, ri, rb in posten]
    i = regels.index(r0)
    return regels[:i] + nieuw + regels[i + 1:], controles


def _tegenrekeningen(koppeling: dict) -> dict:
    return {**laad_koppeling()["tegenrekeningen"], **koppeling.get("tegenrekeningen", {})}


def stel_betalingen_voor(kassa: Kassaomzet, koppeling: dict) -> list[Betaalregel]:
    """Tegenrekening per betaalwijze: eerst de koppeling van de klant, dan de standaard voor de soort."""
    tabel = {_norm(b["betaalwijze"]): str(b.get("rekening") or "").strip()
             for b in koppeling.get("betaalwijzen", []) if isinstance(b, dict) and b.get("betaalwijze")}
    teg = _tegenrekeningen(koppeling)
    uit = []
    for b in kassa.betalingen:
        oms = b.omschrijving or b.naam.lower()
        if _norm(b.naam) in tabel:
            uit.append(Betaalregel(b.naam, b.soort, oms, euro_centen(b.bedrag), tabel[_norm(b.naam)], "koppeling"))
        elif b.soort and str(teg.get(b.soort) or "").strip():
            uit.append(Betaalregel(b.naam, b.soort, oms, euro_centen(b.bedrag), str(teg[b.soort]).strip(), "standaard"))
        else:
            uit.append(Betaalregel(b.naam, b.soort, oms, euro_centen(b.bedrag), "", "nieuw"))
    return uit


def bewaar_koppeling(koppeling: dict, regels: list[Koppelregel], betaalregels: list[Betaalregel] = ()) -> dict:
    """Nieuwe koppeling met de keuzes uit `regels` en `betaalregels` erin (de oude blijft ongewijzigd)."""
    nieuw = copy.deepcopy(koppeling)
    lijst = nieuw.setdefault("categorieen", [])
    for r in regels:
        if r.vast:                                           # tarief uit de koppeling: één regel per categorie
            c = next((c for c in lijst if _norm(c["categorie"]) == _norm(r.categorie)
                      and not str(c.get("tarief") or "")), None)
            waarden = {"omschrijving": r.omschrijving, "rekening": r.rekening.strip(),
                       "btw_tarief": "" if r.rest or r.tarief is None else str(r.tarief), "restpost": r.rest}
            if c is not None:
                c.update(waarden)
            elif r.rekening.strip() or r.rest or r.tarief is not None:
                lijst.append({"categorie": r.categorie, "tarief": "", **waarden})
            continue
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
    teg = _tegenrekeningen(nieuw)
    betaal = nieuw.setdefault("betaalwijzen", [])
    for b in betaalregels:
        rek = b.rekening.strip()
        if not rek:
            continue
        bestaand = next((x for x in betaal if _norm(x.get("betaalwijze")) == _norm(b.naam)), None)
        if bestaand is not None:
            bestaand["rekening"] = rek
        elif rek != str(teg.get(b.soort) or "").strip():          # wijkt af van de standaard voor deze soort
            betaal.append({"betaalwijze": b.naam, "rekening": rek})
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
    soort: str = ""                 # kop, betaling, fooi, verschil, omzet, overig, tegen


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
    lezer: str = ""
    stijl: str = "uitgebreid"
    excl: bool = False              # omzetregels excl. btw (btw-bedrag apart) in plaats van incl.
    bom: bool = True                # CSV met UTF-8-BOM
    crlf: bool = False              # CSV met Windows-regeleinden

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


def standaard_boekstuk(kassa: Kassaomzet, koppeling: dict) -> str:
    prefix = str(koppeling.get("boekstuk_prefix") or "").strip() or prefix_uit_naam(kassa.bedrijf)
    return f"{prefix}{kassa.eind:%m%y}"


def omschrijving_uit_sjabloon(sjabloon: str, kassa: Kassaomzet) -> str:
    """'Omzet {periode}' -> 'Omzet 2026-01'. Ook {maand}, {jaar} en {bedrijf}."""
    waarden = {"periode": f"{kassa.eind:%Y-%m}", "maand": MAANDEN[kassa.eind.month - 1],
               "jaar": str(kassa.eind.year), "bedrijf": kassa.bedrijf}
    return re.sub(r"\{(periode|maand|jaar|bedrijf)\}", lambda m: waarden[m[1]], sjabloon or "Omzet {periode}").strip()


def euro_nl(x: Decimal | None) -> str:
    """Decimal -> '€ 1.234,56'."""
    if x is None:
        return ""
    s = f"{abs(x):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"€ {'-' if x < 0 else ''}{s}"


def maak_boeking(kassa: Kassaomzet, regels: list[Koppelregel], koppeling: dict,
                 betaalregels: list[Betaalregel] | None = None, boekstuk: str | None = None) -> Boeking:
    if betaalregels is None:
        betaalregels = stel_betalingen_voor(kassa, koppeling)
    teg = _tegenrekeningen(koppeling)
    btw_codes = {str(k): str(v) for k, v in koppeling.get("btw_codes", {}).items()}
    stijl = koppeling.get("stijl") or "uitgebreid"
    p = f"{kassa.eind:%Y-%m}"
    boekstuk = (boekstuk or "").strip() or standaard_boekstuk(kassa, koppeling)
    excl = koppeling.get("bedragen") == "excl"
    controles: list[dict] = []
    zonder_tarief = [r for r in regels if r.tarief is None and not r.rest]
    regels, rest_controles = verdeel_rest(regels, kassa)

    betaald = sum((b.bedrag for b in betaalregels), NUL)
    fooi, overige = euro_centen(kassa.fooi), euro_centen(kassa.overige)
    omzet = sum((r.bruto for r in regels), NUL)
    verschil = betaald - omzet - fooi - overige

    tegen = [(f"Omzet {p} - {b.omschrijving}", b.rekening.strip(), -b.bedrag, "betaling") for b in betaalregels if b.bedrag]
    if fooi:
        tegen.append((f"Tip {p}", str(teg["fooi"]).strip(), fooi, "fooi"))
    if verschil:
        tegen.append((f"Omzet {p} - verschil", str(teg["verschil"]).strip(), verschil, "verschil"))
    omzetregels = []
    for r in regels:
        code = btw_codes.get(str(r.tarief), "")
        omzetregels.append(Boekregel(f"Omzet {p} {r.omschrijving}", r.rekening.strip(),
                                     r.netto if excl and code else r.bruto, code, r.btw if code else None, soort="omzet"))
    overig = [(f"Overige inkomsten {p}", str(teg["overige_inkomsten"]).strip(), overige, "overig")] if overige else []

    if stijl == "compact":
        oms = omschrijving_uit_sjabloon(koppeling.get("omschrijving") or "Omzet {periode}", kassa)
        br = [Boekregel(oms, soort="kop")] + [replace(r, omschrijving=oms) for r in omzetregels]
        samen: dict[str, Decimal] = {}
        for _, rek, bedrag, _ in tegen + overig:
            samen[rek] = samen.get(rek, NUL) + bedrag
        br += [Boekregel(oms, rek, bedrag, soort="tegen") for rek, bedrag in samen.items() if bedrag]
    elif stijl == "samengevat":
        # omzetregels met hun eigen omschrijving, daarna de tegenkant per tegenrekening ('Pinbetalingen 2026-10');
        # de rekening waar het verschil op komt als laatste
        br = [Boekregel(f"Omzet {MAANDEN[kassa.eind.month - 1]} {kassa.eind.year} {kassa.bedrijf}", soort="kop")]
        br += omzetregels
        samen, namen = {}, {}
        labels = {"fooi": "Fooi", "verschil": "Verschil", "overig": "Overige inkomsten"}
        for b in betaalregels:
            if b.bedrag:
                namen.setdefault(b.rekening.strip(), SOORT_LABEL.get(b.soort, "Betalingen"))
        for _, rek, bedrag, s in tegen + overig:
            samen[rek] = samen.get(rek, NUL) + bedrag
            namen.setdefault(rek, labels.get(s, "Betalingen"))
        volgorde = sorted(samen, key=lambda rek: verschil and rek == str(teg["verschil"]).strip())
        br += [Boekregel(f"{namen[rek]} {p}", rek, samen[rek], soort="tegen") for rek in volgorde if samen[rek]]
    else:
        br = [Boekregel(f"Omzet {MAANDEN[kassa.eind.month - 1]} {kassa.eind.year} {kassa.bedrijf}", soort="kop")]
        br += [Boekregel(o, rek, bedrag, soort=s) for o, rek, bedrag, s in tegen]
        br += omzetregels
        br += [Boekregel(o, rek, bedrag, soort=s) for o, rek, bedrag, s in overig]

    # -- controles --
    controles.extend(dict(c) for c in kassa.controles)
    controles.extend(rest_controles)
    if zonder_tarief:
        controle(controles, "FOUT", "Btw-tarief per categorie", "het rapport geeft geen tarief; kies er een (of maak "
                 "er de restpost van) voor: " + ", ".join(r.categorie for r in zonder_tarief))
    hele_maand = (kassa.start.day == 1 and (kassa.start.year, kassa.start.month) == (kassa.eind.year, kassa.eind.month)
                  and (kassa.eind + timedelta(days=1)).day == 1)
    controle(controles, "OK" if hele_maand else "LET OP", "Periode is één hele maand",
             (f"{kassa.dagen} dagen; " if kassa.dagen else "") + f"boekingsdatum {kassa.eind:%d-%m-%Y}" if hele_maand else
             f"rapport loopt van {kassa.start:%d-%m-%Y} t/m {kassa.eind:%d-%m-%Y}; geboekt op {kassa.eind:%d-%m-%Y}, "
             f"periode {p}")

    bron = kassa.totalen_bron
    if not kassa.totalen:
        controle(controles, "LET OP", "Omzet per btw-tarief", "het rapport heeft geen btw-overzicht om de omzetregels "
                 "tegen te controleren")
    for t in sorted({r.tarief for r in regels} | set(kassa.totalen), key=_tarief_volgorde):
        incl = sum((r.bruto for r in regels if r.tarief == t), NUL)
        btw = sum((r.btw for r in regels if r.tarief == t), NUL)
        r_incl, r_btw = (euro_centen(x) for x in kassa.totalen.get(t, (NUL, NUL)))
        if not (incl or btw or r_incl or r_btw) or not kassa.totalen:
            continue
        detail = (f"incl. btw {euro_nl(incl)} ({bron} {euro_nl(r_incl)}), btw {euro_nl(btw)} "
                  f"({bron} {euro_nl(r_btw)})")
        if abs(incl - r_incl) > MARGE:
            status = "FOUT"
        elif abs(btw - r_btw) > MARGE:
            status, detail = "LET OP", detail + "; geboekt is de btw over het bedrag incl. btw, de kassa rondt per bon af"
        else:
            status = "OK"
        controle(controles, status, f"Omzet {t}% gelijk aan {bron}", detail)

    if not regels:
        controle(controles, "FOUT", "Omzet gevonden", "het rapport levert geen enkele omzetregel op")
    zonder = [r for r in regels if not r.rekening.strip()]
    ongeldig = [r for r in regels if r.rekening.strip() and not re.fullmatch(r"\d+", r.rekening.strip())]
    if zonder or ongeldig:
        delen = [f"{r.categorie} {r.tarief}%: geen rekening" for r in zonder]
        delen += [f"{r.categorie} {r.tarief}%: '{r.rekening}' is geen rekeningnummer" for r in ongeldig]
        controle(controles, "FOUT", "Elke omzetcategorie heeft een grootboekrekening", "; ".join(delen))
    else:
        nieuw = [r.categorie for r in regels if not r.bekend]
        controle(controles, "OK", "Elke omzetcategorie heeft een grootboekrekening",
                 f"{len(regels)} omzetregels" + (f"; nieuw gekoppeld: {', '.join(dict.fromkeys(nieuw))}" if nieuw else ""))

    zonder_code = [r for r in regels if not btw_codes.get(str(r.tarief)) and r.btw]
    nul = [r for r in regels if r.tarief == 0]
    if zonder_code:
        controle(controles, "FOUT", "Btw-code per tarief",
                 "geen btw-code voor " + ", ".join(sorted({f"{r.tarief}%" for r in zonder_code})))
    elif nul:
        controle(controles, "LET OP", "Btw-code per tarief",
                 "0%-omzet zonder btw-code: " + ", ".join(f"{r.categorie} {euro_nl(r.bruto)}" for r in nul))

    leeg = [b.naam for b in betaalregels if b.bedrag and not b.rekening.strip()]
    leeg += [naam for naam, bedrag, s in (("fooi", fooi, "fooi"), ("verschil", verschil, "verschil"),
                                          ("overige inkomsten", overige, "overige_inkomsten"))
             if bedrag and not str(teg.get(s) or "").strip()]
    fout_nr = [b.naam for b in betaalregels if b.rekening.strip() and not re.fullmatch(r"\d+", b.rekening.strip())]
    if leeg or fout_nr:
        controle(controles, "FOUT", "Tegenrekeningen ingevuld",
                 "; ".join((["geen rekening bij: " + ", ".join(leeg)] if leeg else []) +
                           (["geen rekeningnummer bij: " + ", ".join(fout_nr)] if fout_nr else [])))

    som = sum((b.bedrag for b in br if b.bedrag is not None), NUL)
    if excl:                                             # omzet excl. btw: de btw-bedragen horen erbij
        som += sum((b.btw_bedrag for b in br if b.btw_code and b.btw_bedrag is not None), NUL)
    controle(controles, "OK" if som == 0 else "FOUT", "Boeking sluit op nul",
             f"som van alle regels{' (incl. btw-bedragen)' if excl else ''} {euro_nl(som)}")
    controle(controles, "OK" if not verschil else "LET OP", "Verschil",
             f"{euro_nl(verschil)} op {teg['verschil']}: betalingen {euro_nl(betaald)} min omzet incl. btw "
             f"{euro_nl(omzet)}, fooi {euro_nl(fooi)} en overige inkomsten {euro_nl(overige)}")
    los = [(k, euro_centen(v)) for k, v in kassa.niet_geboekt if euro_centen(v)]
    controle(controles, "OK" if not los else "LET OP", "Niet geboekte posten in het rapport",
             "geen bedragen in posten die niet geboekt worden" if not los else
             "niet apart geboekt: " + ", ".join(f"{k} {euro_nl(v)}" for k, v in los))

    return Boeking(kassa.bedrijf, kassa.start, kassa.eind, boekstuk, str(koppeling.get("dagboek") or "10").strip(), br,
                   controles, omzet, sum((r.btw for r in regels), NUL), betaald, fooi, overige, verschil,
                   kassa.lezer, stijl, excl, koppeling.get("csv_bom", True) is not False,
                   koppeling.get("csv_regeleinde") == "CRLF")


def naar_fmutac(boeking: Boeking) -> bytes:
    """De boeking als FMUTAC-CSV voor de kasboekimport van Asperion (UTF-8, ';', punt als decimaalteken).

    BOM en regeleinden volgen de koppeling van de klant (standaard: met BOM, LF).
    """
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
    eol = "\r\n" if boeking.crlf else "\n"
    return (("\ufeff" if boeking.bom else "") + eol.join(regels) + eol).encode("utf-8")


def asperion_weergave(boeking: Boeking) -> list[dict]:
    """Zo toont Asperion de boeking na het inlezen: omzet excl. btw, aparte btw-regels, debet/credit.

    Plustekens in de CSV zijn credit (omgedraaid), mintekens debet. Handig om naast een schermafdruk te leggen.
    """
    uit = []
    for b in boeking.regels:
        if b.bedrag is None:
            continue
        btw = b.btw_bedrag or NUL
        bedrag = b.bedrag - btw if b.btw_code and not boeking.excl else b.bedrag
        uit.append({"rekening": b.rekening, "debet": -bedrag if bedrag < 0 else NUL,
                    "credit": bedrag if bedrag > 0 else NUL, "btw_code": b.btw_code, "btw": btw if b.btw_code else NUL})
    return uit
