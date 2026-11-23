"""Asperion balans/W&V-rapport inlezen, als Excel (.xlsx) of als PDF.

Beide lezers leveren dezelfde rijen (kolommen A..H zoals in de Excel-export) aan
één gedeelde verwerkingsfunctie, zodat PDF- en Excel-invoer exact dezelfde
uitkomst geven.
"""
from __future__ import annotations

import re
from pathlib import Path

ACCT_RE = re.compile(r"^\s*(\d+)\s*-\s*(.*?)\s*$")
_BEDRAG_RE = re.compile(r"^-?\d{1,3}(\.\d{3})*$")


class InvoerFout(Exception):
    """Het bestand is geen (leesbaar) Asperion balans/W&V-rapport."""


def lees_asperion(pad: Path) -> dict:
    pad = Path(pad)
    ext = pad.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        asp = _verwerk_rijen(_rijen_uit_excel(pad))
    elif ext == ".pdf":
        asp = _verwerk_rijen(_rijen_uit_pdf(pad))
    else:
        raise InvoerFout(f"Onbekend bestandstype '{ext}': gebruik een Asperion-export als .xlsx of .pdf")
    if not asp["rekeningen"] or "periode_huidig" not in asp["meta"]:
        raise InvoerFout("Dit lijkt geen Asperion balans/W&V-rapport: geen grootboekrekeningen of periode gevonden.")
    if set(asp["resultaat"]) != {"balans", "wv"}:
        raise InvoerFout("Het rapport moet zowel de balans als de winst-en-verliesrekening bevatten.")
    return asp


def boekjaar(asp: dict) -> int:
    return int(re.search(r"(\d{4})", asp["meta"]["periode_huidig"]).group(1))


def _num(v) -> int:
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return 0
    return int(round(float(v)))


def _rijen_uit_excel(pad: Path):
    import openpyxl
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # Asperion-export heeft geen standaardstijl
        wb = openpyxl.load_workbook(pad, data_only=True)
    ws = wb.worksheets[0]
    if isinstance(ws["A2"].value, str):
        yield ["Bedrijf:" + ws["A2"].value.strip()] + [None] * 7
    for rij in ws.iter_rows(values_only=True):
        yield (list(rij) + [None] * 8)[:8]


def _rijen_uit_pdf(pad: Path):
    """PDF: bedragen gaan naar een kolom op basis van de rechterkant van de kopwoorden
    'Debet Credit Debet Credit' (huidig/vorig) -> Excel-kolommen D, E, G, H.
    Labels: de inspringing bepaalt sectie (A), subsectie (B) of groep/rekening (C)."""
    import pdfplumber

    with pdfplumber.open(pad) as pdf:
        deel = "balans"
        meta_gezet = bedrijf_gezet = False
        for pagina in pdf.pages:
            pg = pagina.dedupe_chars()  # vette koppen staan dubbel gedrukt
            woorden = pg.extract_words(keep_blank_chars=False, use_text_flow=False)
            kop = sorted((w for w in woorden if w["text"] in ("Debet", "Credit") and w["top"] < 120),
                         key=lambda w: w["x0"])
            if len(kop) != 4:
                raise InvoerFout(f"PDF-pagina {pagina.page_number}: kolomkop 'Debet Credit Debet Credit' niet gevonden")
            kol_x1 = [w["x1"] for w in kop]
            kop_onder = max(w["bottom"] for w in kop)

            regels: list[list] = []
            for w in sorted(woorden, key=lambda w: w["top"]):
                if regels and abs(w["top"] - regels[-1][0]["top"]) <= 2:
                    regels[-1].append(w)
                else:
                    regels.append([w])
            for ws in regels:
                top = ws[0]["top"]
                ws = sorted(ws, key=lambda w: w["x0"])
                tekst = " ".join(w["text"] for w in ws)
                if top < kop_onder:  # paginakop: bedrijf + titel, datum, periode
                    if not bedrijf_gezet and re.search(r"\s(Balans|Winst)", tekst):
                        bedrijf_gezet = True
                        yield ["Bedrijf:" + re.split(r"\s(?:Balans|Winst)", tekst)[0].strip()] + [None] * 7
                    if "Verliesrekening" in tekst and deel == "balans":
                        deel = "wv"
                        yield ["Winst- en Verliesrekening", None, None, None, None, None, None, None]
                    if tekst.startswith("Datum:") and not meta_gezet:
                        yield [tekst.split("Pagina")[0].strip(), None, None, None, None, None, None, None]
                    m = re.findall(r"(\d+-\d{4} t/m \d+-\d{4})", tekst)
                    # vorig jaar staat er als periode ('0-2024 t/m 12-2024') of als 'Gehele boekjaar 2024'
                    los_jaar = re.search(r"t/m \d+-\d{4}\s+(\d{4})\b", tekst)
                    if not meta_gezet and (len(m) == 2 or (len(m) == 1 and los_jaar)):
                        vorig = m[1] if len(m) == 2 else los_jaar.group(1)
                        yield [None, None, None, f"Periode {m[0]}", None, None, f"Periode {vorig}", None]
                        meta_gezet = True
                    continue
                if tekst.startswith("Asperion") or "****" in tekst:  # voettekst / transportregels
                    continue
                label = [w for w in ws if not (_BEDRAG_RE.match(w["text"]) and w["x0"] > 250)]
                bedragen = [w for w in ws if _BEDRAG_RE.match(w["text"]) and w["x0"] > 250]
                rij = [None] * 8
                for w in bedragen:
                    kolom = min(range(4), key=lambda i: abs(w["x1"] - kol_x1[i]))
                    rij[(3, 4, 6, 7)[kolom]] = int(w["text"].replace(".", ""))
                if label:
                    lab = " ".join(w["text"] for w in label)
                    x0 = label[0]["x0"]
                    rij[0 if x0 < 20 else 1 if x0 < 32 else 2] = lab
                yield rij


def _verwerk_rijen(rijen) -> dict:
    deel = "balans"
    sectie = groep = None
    out = {"rekeningen": {}, "groepen": {}, "secties": {}, "resultaat": {}, "totaal": {}, "meta": {}}

    def s(x):
        return x.strip() if isinstance(x, str) else ("" if x is None else x)

    for rij in rijen:
        a, b, c, d, e, f, g, h = (list(rij) + [None] * 8)[:8]
        a, b, c = s(a), s(b), s(c)

        if isinstance(a, str) and a.startswith("Bedrijf:"):
            out["meta"].setdefault("bedrijf", a[len("Bedrijf:"):].strip())
            continue
        if a == "Winst- en Verliesrekening":
            deel = "wv"
            sectie = groep = None
            continue
        if isinstance(d, str) and d.startswith("Periode ") and "t/m" in d:
            out["meta"]["periode_huidig"] = d.strip()
            out["meta"]["periode_vorig"] = s(g)
            continue
        if isinstance(a, str) and a.startswith("Datum:"):
            out["meta"]["exportdatum"] = a.replace("Datum:", "").strip()
        nums = any(isinstance(x, (int, float)) for x in (d, e, g, h))

        if a and not b and not c and not nums and isinstance(a, str) and a.isupper():
            sectie = a
            continue
        if b and not c:
            continue
        if isinstance(c, str) and c:
            m = ACCT_RE.match(c)
            if m:
                nr, naam = m.group(1), m.group(2)
                out["rekeningen"][nr] = {
                    "naam": naam, "deel": deel, "sectie": sectie, "groep": groep,
                    "huidig": _num(d) - _num(e), "vorig": _num(g) - _num(h),
                }
                continue
            if c.startswith("Totaal:"):
                out["totaal"][deel] = {"debet": _num(d), "credit": _num(e),
                                       "debet_vorig": _num(g), "credit_vorig": _num(h)}
                continue
            if c.startswith("Totaal "):
                naam = c[len("Totaal "):].strip()
                out["secties"][(deel, naam)] = {"huidig": _num(d) - _num(e), "vorig": _num(g) - _num(h)}
                continue
            if c.startswith("Resultaat") and "---" in c:
                # Balans: winst staat credit (verlies debet). W&V: winst staat debet als sluitpost
                # (verlies credit). Positief = winst, negatief = verlies.
                hu, vo = _num(d) - _num(e), _num(g) - _num(h)
                teken = -1 if deel == "balans" else 1
                out["resultaat"][deel] = {"huidig": teken * hu, "vorig": teken * vo}
                continue
            if not nums:
                groep = c
                continue
        if not c and nums and groep:
            out["groepen"][(deel, groep)] = {"huidig": _num(d) - _num(e), "vorig": _num(g) - _num(h)}
    return out
