"""Lezer voor jaarrapporten uit het rapportageprogramma van het kantoor.

Leest de opbouw van een bestaand jaarrapport (PDF) uit, zonder AI:
pagina's, tekstregels (met lettergrootte, vet, kleur en positie), lijnen,
en tabellen met kolommen, jaren en rijen. Daarmee kan het rapport later exact
opnieuw worden opgemaakt met nieuwe cijfers.

Kenmerken van het programma (gemeten op meerdere rapporten):
- tabelgebied x ~222-553pt, verdeeld in N gelijke cellen;
- een tabel begint met een grijze lijn (0,5pt) en heeft onder de kop een grijze lijn van 1pt;
- subtotalen: zwarte lijn boven de cel; eindtotaal: lichtgrijze dubbele lijn onder de cel.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

TABEL_X = 215.0                       # alles rechts hiervan in een tabel is een bedrag/kop
_BEDRAG = re.compile(r"^-?\d{1,3}(\.\d{3})*$")
_JAAR = re.compile(r"^(\d{2}-\d{2}-)?(\d{4})$")


def is_bedrag(t: str) -> bool:
    return bool(_BEDRAG.match(t))


def bedrag(t: str) -> int:
    return int(t.replace(".", ""))


@dataclass
class Segment:
    tekst: str
    x0: float
    x1: float


@dataclass
class Regel:
    top: float
    size: float
    vet: bool
    kleur: tuple
    segmenten: list[Segment]
    tabel: int | None = None           # index van de tabel op de pagina, of None

    @property
    def tekst(self) -> str:
        return " ".join(s.tekst for s in self.segmenten)

    @property
    def x0(self) -> float:
        return self.segmenten[0].x0


@dataclass
class Lijn:
    x0: float
    x1: float
    top: float
    hoogte: float
    kleur: tuple


@dataclass
class Afbeelding:
    x0: float
    top: float
    x1: float
    bottom: float
    png: bytes


@dataclass
class Rij:
    id: str
    regel: Regel
    label: str
    waarden: dict[int, str]            # cel-index -> oorspronkelijke tekst van het bedrag
    groep: str = ""                    # vet label erboven binnen de tabel
    kop: str = ""                      # sectiekop boven de tabel


@dataclass
class Tabel:
    pagina: int
    index: int
    top: float
    kop_onder: float
    eind: float
    cellen: list[tuple[float, float]]
    koprijen: list[Regel] = field(default_factory=list)
    rijen: list[Rij] = field(default_factory=list)
    jaren: list[int] = field(default_factory=list)   # één jaar per jaargroep, nieuwste eerst
    kop: str = ""

    @property
    def groep(self) -> int:
        """Aantal cellen per jaar (2 bij balans/W&V met detail- en totaalkolom)."""
        return max(1, len(self.cellen) // max(1, len(self.jaren))) if self.jaren else len(self.cellen)

    def cel(self, x1: float) -> int:
        for i, (a, b) in enumerate(self.cellen):
            if a - 2 <= x1 <= b + 2:
                return i
        return min(range(len(self.cellen)), key=lambda i: abs(self.cellen[i][1] - x1))


@dataclass
class Pagina:
    nr: int
    breedte: float
    hoogte: float
    regels: list[Regel]
    lijnen: list[Lijn]
    tabellen: list[Tabel]
    titel: str = ""
    afbeeldingen: list[Afbeelding] = field(default_factory=list)


@dataclass
class Rapport:
    paginas: list[Pagina]
    verslagjaar: int
    bedrijf: str

    def rijen(self):
        for p in self.paginas:
            for t in p.tabellen:
                for r in t.rijen:
                    yield p, t, r

    @property
    def tabellen(self):
        return [t for p in self.paginas for t in p.tabellen]


# --------------------------------------------------------------------------
def _segmenten(woorden) -> list[Segment]:
    segs: list[Segment] = []
    for w in sorted(woorden, key=lambda w: w["x0"]):
        if segs and w["x0"] - segs[-1].x1 < 6 and not (w["x0"] >= TABEL_X and is_bedrag(w["text"])
                                                         and segs[-1].x1 < TABEL_X):
            segs[-1].tekst += " " + w["text"]
            segs[-1].x1 = w["x1"]
        else:
            segs.append(Segment(w["text"], w["x0"], w["x1"]))
    return segs


def _kleur(c) -> tuple:
    if c is None:
        return (0.0, 0.0, 0.0)
    c = tuple(c) if isinstance(c, (list, tuple)) else (c,)
    return (c[0], c[0], c[0]) if len(c) == 1 else tuple(c[:3])


def _afbeeldingen(pad: Path) -> dict[int, list[Afbeelding]]:
    """Afbeeldingen (logo's) per pagina, met positie, als PNG."""
    import io
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    from . import PDFIUM_SLOT
    uit: dict[int, list[Afbeelding]] = {}
    with PDFIUM_SLOT:
        return _afbeeldingen_lezen(pdfium, pdfium_c, io, pad, uit)


def _afbeeldingen_lezen(pdfium, pdfium_c, io, pad: Path, uit: dict) -> dict[int, list[Afbeelding]]:
    doc = pdfium.PdfDocument(str(pad))
    try:
        for i in range(len(doc)):
            pg = doc[i]
            h = pg.get_height()
            for o in pg.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]):
                try:
                    l, b, r, t = o.get_bounds()
                    buf = io.BytesIO()
                    o.get_bitmap(render=True).to_pil().save(buf, format="PNG")
                    uit.setdefault(i + 1, []).append(Afbeelding(float(l), float(h - t), float(r), float(h - b), buf.getvalue()))
                except Exception:  # noqa: BLE001 - een onleesbare afbeelding mag het rapport niet tegenhouden
                    continue
    finally:
        doc.close()
    return uit


def lees_rapport(pad: Path) -> Rapport:
    import pdfplumber

    afbeeldingen = _afbeeldingen(pad)
    paginas: list[Pagina] = []
    with pdfplumber.open(pad) as pdf:
        for nr, pg in enumerate(pdf.pages, 1):
            pg = pg.filter(lambda o: not (o.get("object_type") == "char" and o.get("size", 0) > 50))  # watermerk
            woorden = pg.extract_words(x_tolerance=1.0, extra_attrs=["fontname", "size", "non_stroking_color"])
            regels: list[Regel] = []
            for w in sorted(woorden, key=lambda w: (round(w["top"], 0), w["x0"])):
                if regels and abs(regels[-1].top - w["top"]) < 1.5:
                    regels[-1]._woorden.append(w)
                else:
                    r = Regel(top=w["top"], size=round(w["size"], 1), vet="Bold" in w["fontname"],
                              kleur=_kleur(w.get("non_stroking_color")), segmenten=[])
                    r._woorden = [w]
                    regels.append(r)
            for r in regels:
                r.segmenten = _segmenten(r._woorden)
                del r._woorden

            lijnen = [Lijn(x0=float(o["x0"]), x1=float(o["x1"]), top=float(o["top"]), hoogte=float(o["height"]),
                           kleur=_kleur(o.get("non_stroking_color"))) for o in pg.rects]
            lijnen = [l for l in lijnen if min(l.kleur) < 0.97]   # witte vlakken (paginagrond) zijn onzichtbaar
            titel = next((r.tekst for r in regels if r.size >= 17.5), "")
            p = Pagina(nr=nr, breedte=float(pg.width), hoogte=float(pg.height), regels=regels, lijnen=lijnen,
                       tabellen=[], titel=titel, afbeeldingen=afbeeldingen.get(nr, []))
            _vind_tabellen(p)
            paginas.append(p)

    # sectiekoppen doorgeven over pagina's heen (een tabel kan bovenaan een pagina staan)
    kop = ""
    for p in paginas:
        for r in p.regels:
            if r.vet and r.size >= 10.8 and r.tabel is None:
                kop = r.tekst
            if r.size >= 17.5:
                kop = r.tekst
        for t in p.tabellen:
            t.kop = _kop_boven(p, t) or kop

    verslagjaar = next((t.jaren[0] for p in paginas for t in p.tabellen if t.jaren), 0)
    bedrijf = paginas[0].regels[1].tekst if len(paginas[0].regels) > 1 else ""
    rap = Rapport(paginas=paginas, verslagjaar=verslagjaar, bedrijf=bedrijf)
    for p, t, r in rap.rijen():
        r.kop = t.kop
    return rap


def _kop_boven(p: Pagina, t: Tabel) -> str:
    kop = ""
    for r in p.regels:
        if r.top >= t.top:
            break
        if (r.vet and r.size >= 10.8) or r.size >= 17.5:
            kop = r.tekst
    return kop


def _is_grijs(l: Lijn) -> bool:
    return 0.4 < l.kleur[0] < 0.6


def _vind_tabellen(p: Pagina) -> None:
    lijnen = sorted([l for l in p.lijnen if l.x0 >= TABEL_X - 5 and l.x1 - l.x0 > 1], key=lambda l: (l.top, l.x0))
    if not lijnen:
        return
    # Tabelbegin: grijze dunne lijn, met binnen 45pt een grijze lijn van 1pt (onderkant van de kop)
    starts = []
    for l in lijnen:
        if _is_grijs(l) and l.hoogte < 0.8 and (not starts or l.top - starts[-1] > 25):
            if any(_is_grijs(m) and m.hoogte > 0.8 and 0 < m.top - l.top < 45 for m in lijnen):
                starts.append(l.top)
    for i, start in enumerate(starts):
        volgende = starts[i + 1] if i + 1 < len(starts) else 10_000
        eigen = [l for l in lijnen if start - 0.5 <= l.top < volgende - 0.5]
        kop_onder = min(l.top for l in eigen if _is_grijs(l) and l.hoogte > 0.8)
        # cellen = de lijnstukken van de kop-onderlijn
        cellen = sorted({(round(l.x0, 1), round(l.x1, 1)) for l in eigen if abs(l.top - kop_onder) < 0.3})
        eind = max(l.top for l in eigen)
        t = Tabel(pagina=p.nr, index=i, top=start, kop_onder=kop_onder, eind=eind, cellen=list(cellen))
        p.tabellen.append(t)

    for t in p.tabellen:
        groep_label = ""
        vorige: Rij | None = None
        for r in p.regels:
            if not (t.top < r.top <= t.eind + 6) or r.size < 9.5:
                continue
            r.tabel = t.index
            if r.top < t.kop_onder:
                t.koprijen.append(r)
                continue
            label_segs = [s for s in r.segmenten if s.x0 < TABEL_X]
            waarde_segs = [s for s in r.segmenten if s.x0 >= TABEL_X and is_bedrag(s.tekst)]
            label = " ".join(s.tekst for s in label_segs)
            if not waarde_segs:
                # vervolgregel van een afgebroken label, of een vet groepslabel
                if vorige is not None and label and r.top - vorige.regel.top < 14.5 and not r.vet:
                    vorige.label = (vorige.label + " " + label).strip()
                elif label:
                    groep_label = label
                continue
            rij = Rij(id=f"p{p.nr}t{t.index}r{len(t.rijen)}", regel=r, label=label,
                      waarden={t.cel(s.x1): s.tekst for s in waarde_segs}, groep=groep_label)
            t.rijen.append(rij)
            vorige = rij
        # jaren uit de kop: de kopregel met jaartallen/data
        for kr in t.koprijen:
            jaren = [int(_JAAR.match(s.tekst).group(2)) for s in kr.segmenten
                     if s.x0 >= TABEL_X and _JAAR.match(s.tekst)]
            if jaren:
                t.jaren = jaren
                break
