"""Doorrollen: een bestaand jaarrapport (PDF) + nieuwe Asperion-balans -> jaarrapport volgend jaar.

Werkt voor elk rapport uit het rapportageprogramma van het kantoor, ongeacht de opbouw:
1. De opbouw van het oude rapport wordt uitgelezen (rapportlezer, zonder AI).
2. Elke regel met een bedrag krijgt een formule in Asperion-bedragen:
   - eerst zoekt de code zelf (precies één Asperion-bedrag geeft de waarde);
   - de rest stelt Claude voor.
   Elke formule wordt BEWEZEN: toegepast op de vorig-jaarkolom van Asperion moet hij de waarde in het oude
   rapport geven (marge 2 euro). Lukt dat niet: één herkansing voor Claude, daarna FOUT.
3. Elke tabel schuift één jaar op (aantal kolommen blijft gelijk); het nieuwe jaar komt vooraan.
4. Het rapport wordt exact opnieuw opgemaakt: zelfde pagina's, teksten en lijnen, nieuwe bedragen en jaartallen.
"""
from __future__ import annotations

import base64
import html
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .ai import AIFout, ClaudeAI, Gebruik
from .berekenen import FOUT, LET_OP, OK
from .inlezen import boekjaar, lees_asperion
from .rapportlezer import TABEL_X, Rapport, Rij, Tabel, bedrag, is_bedrag, lees_rapport
from .render import RenderFout, chrome_pad, chrome_vlaggen
from .structuur import MAANDEN, datum_nl, fmt

MARGE = 2
STAPPEN = {
    "balans": "Nieuwe balans inlezen",
    "rapport": "Oud jaarverslag lezen",
    "koppelen": "Regels koppelen aan Asperion",
    "bewijzen": "Koppelingen bewijzen op vorig jaar",
    "aandachtspunten": "Aandachtspunten bepalen",
    "pdf": "Nieuw jaarverslag opmaken",
}


class DoorrolFout(Exception):
    pass


@dataclass
class Uitkomst:
    rij: Rij
    tabel: Tabel
    offset: int                      # cel binnen de jaargroep
    oud: int                         # waarde in het oude verslagjaar
    termen: list[dict] = field(default_factory=list)
    bron: str = ""                   # "code" of "claude"
    toelichting: str = ""
    bewijs: int | None = None        # formule toegepast op Asperion 'vorig'
    nieuw: int | None = None         # formule toegepast op Asperion 'huidig'
    status: str = ""
    melding: str = ""


@dataclass
class Doorrol:
    rapport: Rapport
    jaar: int
    datum: str
    bedrijf: str
    uitkomsten: dict[str, Uitkomst]
    controles: list[dict]
    signalen: list[dict]
    aandachtspunten: list[dict]
    aandachtspunten_bron: str
    gebruik: list[Gebruik]
    kerncijfers: list[tuple[str, int, int]]
    asperion_meta: dict
    bronnen: dict = field(default_factory=dict)
    pdf: Path | None = None
    definitief: Path | None = None
    rapport_md: Path | None = None
    waarschuwingen: list[str] = field(default_factory=list)
    instructies: str = ""
    asperion: dict = field(default_factory=dict)
    kantoor: dict = field(default_factory=dict)
    tekstwijzigingen: dict[str, str] = field(default_factory=dict)   # regel-id -> nieuwe tekst
    aanpassingen: list[dict] = field(default_factory=list)           # chatlog: vraag, antwoord, wijzigingen

    @property
    def status_telling(self) -> dict:
        t = {"OK": 0, "LET OP": 0, "FOUT": 0}
        for k in self.controles:
            t[k["status"]] += 1
        return t

    @property
    def kosten_usd(self) -> float:
        return round(sum(g.kosten_usd for g in self.gebruik), 5)


# --------------------------------------------------------------------------
# Asperion-bronnen en formules
# --------------------------------------------------------------------------
def bronnen(asp: dict) -> dict[str, dict]:
    b: dict[str, dict] = {}
    for nr, r in asp["rekeningen"].items():
        b[f"rekening:{nr}"] = {"omschrijving": f"{nr} {r['naam']} ({r['groep']})", "huidig": r["huidig"],
                               "vorig": r["vorig"], "deel": r["deel"], "groep": r["groep"], "sectie": r["sectie"]}
    for (deel, g), v in asp["groepen"].items():
        b[f"groep:{deel}|{g}"] = {"omschrijving": f"groep {g} ({deel})", **v}
    for (deel, s), v in asp["secties"].items():
        b[f"sectie:{deel}|{s}"] = {"omschrijving": f"Totaal {s} ({deel})", **v}
    b["resultaat"] = {"omschrijving": "resultaat boekjaar (winst +, verlies -)",
                      "huidig": asp["resultaat"]["wv"]["huidig"], "vorig": asp["resultaat"]["wv"]["vorig"]}
    return b


def _zoek(bron: str, naam: str, B: dict) -> str | None:
    naam = naam.strip()
    if bron in ("rekening", "aflossing"):
        nr = re.match(r"\s*(\d+)", naam)
        return f"rekening:{nr.group(1)}" if nr and f"rekening:{nr.group(1)}" in B else None
    sleutel = f"{bron}:{naam}"
    if sleutel in B:
        return sleutel
    laag = naam.lower().split("|")[-1].strip()
    kandidaten = [k for k in B if k.startswith(bron + ":") and k.split("|")[-1].lower() == laag]
    return kandidaten[0] if len(kandidaten) == 1 else None


class _Onbekend(Exception):
    pass


def evalueer(termen: list[dict], kolom: str, B: dict, rijwaarden: dict[str, int | None]) -> int | None:
    """kolom 'vorig' = bewijs op het oude jaar; 'huidig' = nieuwe waarde. None = niet te bepalen."""
    totaal = 0
    for t in termen:
        bron, teken = t["bron"], int(t["teken"])
        if bron == "resultaat":
            v = B["resultaat"][kolom]
        elif bron == "rij":
            v = rijwaarden.get(t["naam"].strip())
            if v is None:
                return None
        elif bron == "aflossing":
            k = _zoek(bron, t["naam"], B)
            if k is None:
                raise _Onbekend(t["naam"])
            if kolom == "vorig":
                return None                      # saldo van twee jaar terug staat niet in de export
            v = B[k]["huidig"] - B[k]["vorig"]
        else:
            k = _zoek(bron, t["naam"], B)
            if k is None:
                raise _Onbekend(f"{bron} {t['naam']}")
            v = B[k][kolom]
        totaal += teken * v
    return totaal


def formule_tekst(termen: list[dict], B: dict) -> str:
    if not termen:
        return "nihil"
    delen = []
    for t in termen:
        k = _zoek(t["bron"], t["naam"], B) if t["bron"] not in ("resultaat", "rij") else None
        naam = {"resultaat": "resultaat", "rij": f"regel {t['naam']}"}.get(t["bron"]) or (
            ("aflossing " if t["bron"] == "aflossing" else "") + (B[k]["omschrijving"] if k else t["naam"]))
        delen.append(("- " if int(t["teken"]) < 0 else "+ ") + naam)
    s = " ".join(delen)
    return s[2:] if s.startswith("+ ") else s


# --------------------------------------------------------------------------
# Koppelen en bewijzen
# --------------------------------------------------------------------------
def _oude_waarde(t: Tabel, r: Rij) -> tuple[int, int] | None:
    for cel in range(t.groep):
        if cel in r.waarden:
            return cel, bedrag(r.waarden[cel])
    return None


_STOP = {"totaal", "overige", "overig", "van", "de", "het", "en", "op", "te", "per", "voor", "na"}


def _woorden(t: str) -> set[str]:
    import unicodedata
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    return {w for w in re.findall(r"[a-z]{4,}", t) if w not in _STOP}


def zelfde_bedrijf(a: str, b: str) -> bool:
    """Bedrijfsnamen vergelijken, ongevoelig voor hoofdletters, puntjes en rechtsvorm (B.V./BV)."""
    from difflib import SequenceMatcher
    norm = lambda t: re.sub(r"\b(b ?v|n ?v|v ?o ?f)\b", "", re.sub(r"[^a-z0-9 ]", " ", t.lower().replace(".", ""))).split()  # noqa: E731
    x, y = " ".join(norm(a)), " ".join(norm(b))
    return x == y or SequenceMatcher(None, x, y).ratio() >= 0.85


def lijkt_op(label: str, omschrijving: str) -> bool:
    """Minstens één inhoudelijk woord gelijk (spelling mag iets afwijken: activa/aktiva)."""
    from difflib import SequenceMatcher
    a, b = _woorden(label), _woorden(omschrijving)
    return any(x == y or x in y or y in x or SequenceMatcher(None, x, y).ratio() >= 0.8 for x in a for y in b)


def _auto(oud: int, B: dict, label: str = "") -> tuple[list[dict], str] | None:
    if abs(oud) < 100:
        return None                              # te veel toevallige treffers bij kleine bedragen
    marge = 1 if abs(oud) >= 1000 else 0
    kand = [(k, s) for k, it in B.items() for s in (1, -1)
            if abs(s * it["vorig"] - oud) <= marge and lijkt_op(label, it["omschrijving"])]
    if not kand or len({s * B[k]["huidig"] for k, s in kand}) != 1:
        return None
    kand.sort(key=lambda ks: (abs(ks[1] * B[ks[0]]["vorig"] - oud), ks[0].startswith("rekening")))
    k, s = kand[0]
    bron, _, naam = k.partition(":")
    termen = [{"bron": bron, "naam": naam, "teken": s}] if bron != "resultaat" else [{"bron": "resultaat", "naam": "", "teken": s}]
    return termen, f"bedrag gevonden: {B[k]['omschrijving']}"


def _bewijs(uitkomsten: dict[str, Uitkomst], B: dict) -> None:
    """Bereken bewijs (vorig) en nieuw (huidig) voor alle regels, met afhankelijkheden via 'rij'-termen."""
    oud = {i: u.oud for i, u in uitkomsten.items()}
    nieuw: dict[str, int | None] = {}
    open_ = [i for i, u in uitkomsten.items() if u.bron]
    for _ in range(len(open_) + 1):
        voortgang = False
        for i in list(open_):
            u = uitkomsten[i]
            if any(t["bron"] == "rij" and t["naam"] in open_ and t["naam"] != i for t in u.termen):
                continue
            try:
                u.bewijs = evalueer(u.termen, "vorig", B, oud)
                u.nieuw = evalueer(u.termen, "huidig", B, nieuw)
            except _Onbekend as e:
                u.bewijs = u.nieuw = None
                u.melding = f"onbekende bron: {e}"
            nieuw[i] = u.nieuw
            open_.remove(i)
            voortgang = True
        if not open_ or not voortgang:
            break
    for i in open_:                              # kringverwijzing
        uitkomsten[i].melding = "kringverwijzing tussen regels"


def _klopt(bewijs: int, oud: int) -> bool:
    """Gelijk binnen de afrondingsmarge, en nooit met omgedraaid teken (1 vs -1 is geen afronding)."""
    if abs(bewijs - oud) > MARGE:
        return False
    return oud == 0 or bewijs == 0 or (bewijs > 0) == (oud > 0) if abs(oud) > MARGE else bewijs == oud


def _status(u: Uitkomst) -> None:
    if not u.bron:
        u.status, u.melding = FOUT, u.melding or "geen koppeling gevonden"
    elif u.nieuw is None:
        u.status = FOUT
        u.melding = u.melding or "nieuwe waarde niet te berekenen"
    elif u.bewijs is None:
        u.status, u.melding = LET_OP, u.melding or "niet te controleren op vorig jaar (aflossing)"
    elif _klopt(u.bewijs, u.oud):
        u.status = OK
    else:
        u.status, u.melding = FOUT, f"formule geeft {fmt(u.bewijs)} voor {u.tabel.jaren[0]}, rapport {fmt(u.oud)}"


def koppel(rap: Rapport, asp: dict, ai: ClaudeAI | None, stap=lambda s, d="": None,
           instructies: str = "") -> dict[str, Uitkomst]:
    B = bronnen(asp)
    uitkomsten: dict[str, Uitkomst] = {}
    for p, t, r in rap.rijen():
        ow = _oude_waarde(t, r)
        if ow is None:
            continue
        uitkomsten[r.id] = Uitkomst(rij=r, tabel=t, offset=ow[0], oud=ow[1])

    for u in uitkomsten.values():
        a = _auto(u.oud, B, u.rij.label or u.rij.groep)
        if a:
            u.termen, u.toelichting = a
            u.bron = "code"
    n_code = sum(1 for u in uitkomsten.values() if u.bron)
    stap("koppelen", f"{n_code} van {len(uitkomsten)} regels door de code gevonden")

    def vraag_claude(ids: list[str], feedback=None):
        rijen = [{"id": i, "pagina": uitkomsten[i].tabel.kop and p_titel(rap, uitkomsten[i].tabel) or "",
                  "kop": uitkomsten[i].tabel.kop, "groep": uitkomsten[i].rij.groep,
                  "label": uitkomsten[i].rij.label or "(totaalregel)", "waarde_verslagjaar": uitkomsten[i].oud}
                 for i in ids]
        bekend = [{"id": i, "label": u.rij.label or f"totaal {u.rij.groep}", "waarde_verslagjaar": u.oud}
                  for i, u in uitkomsten.items() if u.bron and i not in ids]
        bron_lijst = [{"sleutel": k, "omschrijving": v["omschrijving"], "vorig": v["vorig"]} for k, v in B.items()]
        for f in ai.koppel_regels(rijen, bron_lijst, bekend, feedback, instructies=instructies):
            u = uitkomsten[f["rij"]]
            u.termen, u.toelichting, u.bron = f["termen"], f["toelichting"], "claude"

    open_ids = [i for i, u in uitkomsten.items() if not u.bron]
    if open_ids:
        if ai is None:
            raise DoorrolFout(f"{len(open_ids)} regels kon de code niet zelf koppelen; hiervoor is Claude nodig "
                              "(vul de API-sleutel in).")
        vraag_claude(open_ids)
        stap("koppelen", f"{len(open_ids)} regels door Claude gekoppeld")

    stap("bewijzen", "")
    _bewijs(uitkomsten, B)
    # automatisch teken omdraaien als de formule precies het tegengestelde geeft
    for u in uitkomsten.values():
        if u.bewijs is not None and u.oud != 0 and not _klopt(u.bewijs, u.oud) and _klopt(-u.bewijs, u.oud):
            u.termen = [{**t, "teken": -int(t["teken"])} for t in u.termen]
    _bewijs(uitkomsten, B)
    for u in uitkomsten.values():
        _status(u)

    fout = [i for i, u in uitkomsten.items() if u.status == FOUT]
    if fout and ai is not None:                  # één herkansing met feedback
        feedback = [{"id": i, "formule": uitkomsten[i].termen, "uitkomst_vorig": uitkomsten[i].bewijs,
                     "moet_zijn": uitkomsten[i].oud} for i in fout]
        try:
            vraag_claude(fout, feedback)
            _bewijs(uitkomsten, B)
            for u in uitkomsten.values():
                _status(u)
        except AIFout:
            pass
    n_ok = sum(1 for u in uitkomsten.values() if u.status == OK)
    stap("bewijzen", f"{n_ok} van {len(uitkomsten)} regels bewezen op {rap.verslagjaar}")
    return uitkomsten


def p_titel(rap: Rapport, t: Tabel) -> str:
    return next((p.titel for p in rap.paginas if p.nr == t.pagina), "")


# --------------------------------------------------------------------------
# Controles, kerncijfers en signalen
# --------------------------------------------------------------------------
def _is_overzicht(t: Tabel) -> bool:
    k = t.kop.lower()
    return (k.startswith("balans") and "meerjaren" not in k) or k == "winst-en-verliesrekening"


def kerncijfers(uitkomsten: dict[str, Uitkomst]) -> dict[str, tuple[str, int, int]]:
    """Omzet, resultaat, eigen vermogen en balanstotaal uit de overzichtstabellen (indien aanwezig)."""
    uit: dict[str, tuple[str, int, int]] = {}
    for u in uitkomsten.values():
        if not _is_overzicht(u.tabel) or u.nieuw is None:
            continue
        lab, groep = u.rij.label.lower(), u.rij.groep.lower()
        if lab.startswith("omzet") and "omzet" not in uit:
            uit["omzet"] = ("Omzet", u.nieuw, u.oud)
        if lab.startswith("resultaat") and "winst" in u.tabel.kop.lower() + " winst":
            uit["resultaat"] = ("Resultaat", u.nieuw, u.oud)
        if (lab in ("eigen vermogen", "kapitaal") or (not lab and groep == "eigen vermogen")) and u.rij.regel.vet:
            uit["ev"] = ("Eigen vermogen", u.nieuw, u.oud)
        if lab == "kapitaal" and "ev" not in uit:
            uit["ev"] = ("Eigen vermogen", u.nieuw, u.oud)
        if lab == "activa":
            uit["activa"] = ("Balanstotaal", u.nieuw, u.oud)
    return uit


def controles(rap: Rapport, asp: dict, uitkomsten: dict[str, Uitkomst], jaar: int) -> list[dict]:
    c: list[dict] = []

    def ctrl(status, tekst, detail, kort=None):
        c.append({"status": status, "controle": tekst, "detail": detail, **({"kort": kort} if kort else {})})

    ctrl(OK if rap.verslagjaar == jaar - 1 else FOUT, "Oud jaarverslag hoort bij de balans",
         f"jaarverslag {rap.verslagjaar}, balans {jaar}",
         kort=("Jaartal klopt niet", f"Jaarverslag {rap.verslagjaar} hoort niet bij balans {jaar}."))
    ctrl(OK, "Oud jaarverslag gelezen",
         f"{len(rap.paginas)} pagina's, {len(rap.tabellen)} tabellen, {len(uitkomsten)} regels met bedragen")
    n = {s: [u for u in uitkomsten.values() if u.status == s] for s in (OK, LET_OP, FOUT)}
    n_code = sum(1 for u in uitkomsten.values() if u.bron == "code")
    ctrl(FOUT if n[FOUT] else OK, f"Elke regel gekoppeld en bewezen op {rap.verslagjaar}",
         f"{len(n[OK])} bewezen ({n_code} door code, {len(n[OK]) - sum(1 for u in n[OK] if u.bron == 'code')} door Claude)"
         + (f"; FOUT: " + "; ".join(f"{u.rij.label or 'totaal ' + u.rij.groep} (p. {u.tabel.pagina}): {u.melding}"
                                    for u in n[FOUT]) if n[FOUT] else ""),
         kort=(f"{len(n[FOUT])} regel(s) niet bewezen", "Controleer deze regels in het controlerapport."))
    if n[LET_OP]:
        ctrl(LET_OP, "Regels die niet op vorig jaar te controleren zijn",
             "; ".join(f"{u.rij.label or 'totaal ' + u.rij.groep}: {u.melding}" for u in n[LET_OP]),
             kort=("Schatting in het rapport", f"{len(n[LET_OP])} regel(s) niet te bewijzen op vorig jaar; controleer."))

    # Dekking: komt elke Asperion-rekening (met saldo) via een formule in het rapport?
    B = bronnen(asp)
    gebruikt_rek, gebruikt_groep, gebruikt_sectie, resultaat = set(), set(), set(), False
    for u in uitkomsten.values():
        for t in u.termen:
            k = _zoek(t["bron"], t["naam"], B) if t["bron"] not in ("resultaat", "rij") else None
            if t["bron"] == "resultaat":
                resultaat = True
            elif k and k.startswith("rekening:"):
                gebruikt_rek.add(k.split(":")[1])
            elif k and k.startswith("groep:"):
                gebruikt_groep.add(k.split(":", 1)[1])
            elif k and k.startswith("sectie:"):
                gebruikt_sectie.add(k.split(":", 1)[1])
    los = [f"{nr} {r['naam']}" for nr, r in asp["rekeningen"].items()
           if (r["huidig"] or r["vorig"]) and nr not in gebruikt_rek
           and f"{r['deel']}|{r['groep']}" not in gebruikt_groep and f"{r['deel']}|{r['sectie']}" not in gebruikt_sectie
           and not (r["deel"] == "wv" and resultaat)]
    ctrl(LET_OP if los else OK, "Elke Asperion-rekening komt in het rapport terug",
         f"niet teruggevonden: {', '.join(los)}" if los else f"{len(asp['rekeningen'])} rekeningen",
         kort=("Rekening niet in rapport", f"{', '.join(los)}; controleer of die mee moet."))
    ctrl(OK if all(t["debet"] == t["credit"] for t in asp["totaal"].values()) else FOUT,
         "Totaal-debet = totaal-credit in de Asperion-export",
         "; ".join(f"{k}: {fmt(t['debet'])} / {fmt(t['credit'])}" for k, t in asp["totaal"].items()))

    # Optellingen: een vette regel zonder omschrijving direct na detailregels is hun (sub)totaal.
    # De verhouding (som of tegengestelde som) leren we uit het oude rapport en eisen we in het nieuwe.
    afwijkend, n_sommen = [], 0
    for t in rap.tabellen:
        detail: list[Uitkomst] = []
        for r in t.rijen:
            u = uitkomsten.get(r.id)
            if u is None:
                continue
            if r.regel.vet and not r.label and detail:
                som_oud, som_nieuw = sum(d.oud for d in detail), sum((d.nieuw or 0) for d in detail)
                teken = 1 if abs(u.oud - som_oud) <= MARGE else -1 if abs(u.oud + som_oud) <= MARGE else 0
                if teken and u.nieuw is not None:
                    n_sommen += 1
                    verschil = u.nieuw - teken * som_nieuw
                    if verschil:
                        afwijkend.append((abs(verschil), f"p. {t.pagina} {t.kop} / {r.groep or 'totaal'}: "
                                                         f"regels {fmt(teken * som_nieuw)}, totaal {fmt(u.nieuw)}"))
                detail = []
            elif not r.regel.vet:
                detail.append(u)
            else:
                detail = []
    ernstig = [t for v, t in afwijkend if v > MARGE]
    ctrl(FOUT if ernstig else LET_OP if afwijkend else OK, "Tabellen tellen op (zoals in het oude rapport)",
         (f"{n_sommen} optellingen gecontroleerd" if not afwijkend else "; ".join(t for _, t in afwijkend))
         + ("" if ernstig or not afwijkend else " (afronding)"),
         kort=("Tabel telt niet op", "Totaal wijkt af van de regels; controleer de koppeling.") if ernstig
         else ("Afrondingsverschil in tabel", "Regels en totaal wijken 1-2 euro af; cijfers niet aangepast."))

    # Dezelfde regel (zelfde omschrijving en zelfde bedrag vorig jaar) moet overal hetzelfde nieuwe bedrag krijgen
    per_regel: dict[tuple[str, int], list[Uitkomst]] = {}
    for u in uitkomsten.values():
        if u.rij.label and u.nieuw is not None:
            per_regel.setdefault((u.rij.label.lower(), u.oud), []).append(u)
    strijdig = [f"{us[0].rij.label}: " + ", ".join(f"p. {u.tabel.pagina} {fmt(u.nieuw)}" for u in us)
                for us in per_regel.values() if len({u.nieuw for u in us}) > 1]
    ctrl(FOUT if strijdig else OK, "Dezelfde regel heeft overal hetzelfde bedrag",
         "; ".join(strijdig) if strijdig else f"{sum(1 for us in per_regel.values() if len(us) > 1)} regels in "
                                              "meerdere tabellen vergeleken",
         kort=("Regel wijkt af tussen tabellen", "Zelfde post, verschillende bedragen; controleer de koppeling."))

    # Regels die nu in alle kolommen nihil zijn: het sjabloon houdt ze vast, de accountant beslist
    nul = []
    for t in rap.tabellen:
        cellen = nieuwe_tabelcellen(t, uitkomsten)
        for r in t.rijen:
            waarden = [w for w in cellen.get(r.id, {}).values() if w != "?"]
            if r.label and waarden and all(bedrag(w) == 0 for w in waarden):
                nul.append(f"{r.label} (p. {t.pagina})")
    if nul:
        ctrl(LET_OP, "Regels die in alle kolommen nihil zijn", ", ".join(nul),
             kort=("Lege regels in het rapport", f"{', '.join(dict.fromkeys(n.split(' (')[0] for n in nul))}: "
                                                 "overal nihil; eventueel weglaten."))

    act = [u for u in uitkomsten.values() if u.rij.label.lower() == "activa" and _is_overzicht(u.tabel)]
    pas = [u for u in uitkomsten.values() if u.rij.label.lower() == "passiva" and _is_overzicht(u.tabel)]
    if act and pas and act[0].nieuw is not None and pas[0].nieuw is not None:
        v = act[0].nieuw - pas[0].nieuw
        ctrl(LET_OP if v else OK, "Balanstotaal activa en passiva",
             f"activa {fmt(act[0].nieuw)} / passiva {fmt(pas[0].nieuw)}"
             + (f"; verschil {fmt(v)} door afronding in Asperion, niet aangepast" if v else ""),
             kort=(f"Balans wijkt € {fmt(abs(v))} af", "Afronding in Asperion; cijfers niet aangepast."))
    return c


def signalen(uitkomsten: dict[str, Uitkomst], ctrl: list[dict], kern: dict, kantoor: dict) -> list[dict]:
    drempel = int(kantoor.get("signaal_drempel_bedrag", 5000))
    pct_drempel = float(kantoor.get("signaal_drempel_procent", 25))
    s: list[dict] = []
    for k in ctrl:
        if k["status"] in (LET_OP, FOUT):
            titel, tekst = k.get("kort") or (k["controle"], "Los dit op voordat je vaststelt.")
            s.append({"ernst": "hoog" if k["status"] == FOUT else "laag", "titel": titel, "tekst": tekst})
    gezien = set()
    for u in uitkomsten.values():
        if not _is_overzicht(u.tabel) or not u.rij.label or u.nieuw is None or u.rij.label in gezien:
            continue
        gezien.add(u.rij.label)
        if u.nieuw == 0 and abs(u.oud) >= drempel:
            s.append({"ernst": "middel", "titel": f"{u.rij.label} nihil",
                      "tekst": f"Vorig jaar {fmt(u.oud)}. Navragen of dit compleet is."})
        elif abs(u.nieuw - u.oud) >= drempel and (u.oud == 0 or abs((u.nieuw - u.oud) / u.oud * 100) >= pct_drempel):
            pct = f" {(u.nieuw - u.oud) / abs(u.oud) * 100:+.0f}%" if u.oud else ""
            s.append({"ernst": "laag", "titel": f"{u.rij.label}{pct}",
                      "tekst": f"Van {fmt(u.oud)} naar {fmt(u.nieuw)}."})
    if "ev" in kern and kern["ev"][1] < 0:
        s.append({"ernst": "hoog", "titel": "Eigen vermogen negatief",
                  "tekst": f"{fmt(kern['ev'][1])}; continuïteit en eventuele achterstelling beoordelen."})
    if "resultaat" in kern and kern["resultaat"][1] < 0:
        s.append({"ernst": "middel", "titel": "Verlies over het boekjaar",
                  "tekst": f"Resultaat {fmt(kern['resultaat'][1])} (vorig jaar {fmt(kern['resultaat'][2])})."})
    return s


# --------------------------------------------------------------------------
# Opmaak: het oude rapport exact opnieuw zetten met nieuwe bedragen en jaartallen
# --------------------------------------------------------------------------
_DATUM = re.compile(r"\b\d{1,2} (" + "|".join(MAANDEN) + r") \d{4}\b")
_JAAR_TOKEN = re.compile(r"^(\d{2}-\d{2}-)?(\d{4})$")
_PAD = 5.4                                       # rechter binnenmarge van bedragen in een cel


def _vervang_tekst(t: str, vorige_seg: str, oud_jaar: int, datum: str) -> str:
    if _DATUM.fullmatch(t.strip()) and vorige_seg.strip().lower().startswith("gegenereerd"):
        return datum
    m = re.fullmatch(r"([A-Z][\w .'-]{1,40}), " + _DATUM.pattern, t.strip())
    if m:                                        # plaats + datum (ondertekening)
        return f"{m.group(1)}, {datum}"
    return re.sub(rf"\b{oud_jaar}\b", str(oud_jaar + 1), t)


def _kleur_css(k: tuple) -> str:
    return "rgb({},{},{})".format(*(int(round(max(0.0, min(1.0, x)) * 255)) for x in k))


def _cel_tekst(v: int | None) -> str:
    return "?" if v is None else fmt(v)


def nieuwe_tabelcellen(t: Tabel, uitkomsten: dict[str, Uitkomst]) -> dict[str, dict[int, str]]:
    """Per rij: nieuwe celteksten. Jaargroep 0 = nieuw jaar, de rest schuift één groep op."""
    g, n = t.groep, len(t.cellen)
    uit = {}
    for r in t.rijen:
        cellen: dict[int, str] = {}
        for cel, tekst in r.waarden.items():         # oude kolommen één jaargroep opschuiven
            if cel + g < n:
                cellen[cel + g] = tekst
        u = uitkomsten.get(r.id)
        if u is not None:
            cellen[u.offset] = _cel_tekst(u.nieuw)
        uit[r.id] = cellen
    return uit


def _nieuwe_koppen(t: Tabel) -> dict[int, dict[int, str]]:
    """Kopregels: jaartallen schuiven; teksten als 'Verslagjaar' blijven op hun plek."""
    g, n = t.groep, len(t.cellen)
    uit = {}
    for ki, kr in enumerate(t.koprijen):
        oude = {t.cel(s.x1): s.tekst for s in kr.segmenten if s.x0 >= TABEL_X}
        if not any(_JAAR_TOKEN.match(v) for v in oude.values()):
            continue
        nieuw = {}
        for cel, tekst in oude.items():
            if cel + g < n:
                nieuw[cel + g] = tekst
            if cel < g:
                m = _JAAR_TOKEN.match(tekst)
                nieuw[cel] = f"{m.group(1) or ''}{int(m.group(2)) + 1}" if m else tekst
        uit[ki] = nieuw
    return uit


def tekstregels(rap: Rapport, datum: str) -> dict[str, tuple]:
    """Alle regels buiten de bedragentabellen: id -> (pagina, regel, tekst zoals in het nieuwe rapport)."""
    uit: dict[str, tuple] = {}
    for p in rap.paginas:
        tabellen = {t.index: t for t in p.tabellen}
        rijen = {id(r.regel) for t in p.tabellen for r in t.rijen}
        for i, regel in enumerate(p.regels):
            t = tabellen.get(regel.tabel) if regel.tabel is not None else None
            if t is not None and (regel in t.koprijen or id(regel) in rijen):
                continue
            delen, vorige = [], ""
            for sg in regel.segmenten:
                delen.append(_vervang_tekst(sg.tekst, vorige, rap.verslagjaar, datum))
                vorige = sg.tekst
            uit[f"p{p.nr}l{i}"] = (p.nr, regel, " ".join(delen))
    return uit


def inhoud_links(rap: Rapport, p) -> list[tuple[int, float, float, float, float]]:
    """Klikbare inhoudsopgave: elke regel op de inhoudsopgave-pagina die eindigt op een paginanummer
    wordt een link naar die pagina. Geeft (pagina, x0, top, x1, hoogte) in pt."""
    if not (p.titel or "").lower().startswith("inhoud"):
        return []
    links = []
    for regel in p.regels:
        segs = regel.segmenten
        if len(segs) < 2 or not segs[-1].tekst.isdigit() or regel.size > 14:
            continue
        n = int(segs[-1].tekst)
        if p.nr < n <= len(rap.paginas):
            links.append((n, segs[0].x0 - 2, regel.top - 2, segs[-1].x1 + 2, regel.size + 4))
    return links


def render_html(rap: Rapport, uitkomsten: dict[str, Uitkomst], datum: str, concept: bool,
                tekst: dict[str, str] | None = None) -> str:
    tekst = tekst or {}
    oud_jaar = rap.verslagjaar
    delen = ["""<!doctype html><html lang="nl"><head><meta charset="utf-8"><style>
@page { size: A4; margin: 0; }
html, body { margin: 0; padding: 0; background: #fff; }
body { font-family: Helvetica, Arial, sans-serif; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.pg { width: 210mm; height: 297mm; position: relative; overflow: hidden; break-after: page; }
.pg:last-child { break-after: auto; }
.t { position: absolute; white-space: pre; line-height: 1; }
.rr { transform: translateX(-100%); }
.l { position: absolute; }
.lijnen { position: absolute; left: 0; top: 0; }
.toc { position: absolute; display: block; }
.wm { position: absolute; left: 288pt; top: 405pt; transform: translate(-50%, -50%) rotate(-45deg);
      font-family: Arial, Helvetica, sans-serif; font-size: 96pt; color: rgba(0,0,0,0.21); white-space: nowrap; }
</style></head><body>"""]
    # Verschuiving: pdfplumber 'top' is de bovenkant van de tekstbox; CSS line-height 1 zet de glyph iets lager.
    dy = {24.0: -0.9, 18.0: -0.75, 11.0: -0.6, 10.0: -0.6, 9.0: -0.55}   # gemeten tegen het origineel

    def tekst_div(s: str, x: float, top: float, size: float, vet: bool, kleur: tuple, rechts: bool) -> str:
        return (f'<div class="t{" rr" if rechts else ""}" style="left:{x:.2f}pt;top:{top + dy.get(size, 0):.2f}pt;'
                f'font-size:{size}pt;{"font-weight:bold;" if vet else ""}color:{_kleur_css(kleur)}">'
                f"{html.escape(s)}</div>")

    for p in rap.paginas:
        delen.append(f'<section class="pg" id="p{p.nr}">')
        for a in p.afbeeldingen:
            delen.append(f'<img class="l" style="left:{a.x0:.2f}pt;top:{a.top:.2f}pt;width:{a.x1 - a.x0:.2f}pt;'
                         f'height:{a.bottom - a.top:.2f}pt" src="data:image/png;base64,{base64.b64encode(a.png).decode()}">')
        # Lijnen als SVG (vector): exacte diktes blijven behouden, zodat dit rapport volgend jaar
        # zelf weer als 'oud rapport' kan worden ingelezen (HTML-divs worden op pixels afgerond).
        delen.append(f'<svg class="lijnen" width="{p.breedte}pt" height="{p.hoogte}pt" '
                     f'viewBox="0 0 {p.breedte} {p.hoogte}" xmlns="http://www.w3.org/2000/svg">')
        for l in p.lijnen:
            delen.append(f'<rect x="{l.x0:.2f}" y="{l.top:.2f}" width="{max(l.x1 - l.x0, 0.1):.2f}" '
                         f'height="{max(l.hoogte, 0.1):.2f}" fill="{_kleur_css(l.kleur)}" shape-rendering="crispEdges"/>')
        delen.append("</svg>")
        tabellen = {t.index: t for t in p.tabellen}
        cellen_per_tabel = {t.index: nieuwe_tabelcellen(t, uitkomsten) for t in p.tabellen}
        koppen_per_tabel = {t.index: _nieuwe_koppen(t) for t in p.tabellen}
        rij_per_regel = {id(r.regel): r for t in p.tabellen for r in t.rijen}
        for i, regel in enumerate(p.regels):
            t = tabellen.get(regel.tabel) if regel.tabel is not None else None
            if t is not None and regel in t.koprijen:
                ki = t.koprijen.index(regel)
                for s in regel.segmenten:
                    if s.x0 < TABEL_X:
                        delen.append(tekst_div(s.tekst, s.x0, regel.top, regel.size, regel.vet, regel.kleur, False))
                if ki in koppen_per_tabel[t.index]:
                    for cel, tekst in koppen_per_tabel[t.index][ki].items():
                        x1 = t.cellen[cel][1] - _PAD
                        delen.append(tekst_div(tekst, x1, regel.top, regel.size, regel.vet, regel.kleur, True))
                else:
                    for s in regel.segmenten:
                        if s.x0 >= TABEL_X:
                            delen.append(tekst_div(s.tekst, s.x1, regel.top, regel.size, regel.vet, regel.kleur, True))
                continue
            rij = rij_per_regel.get(id(regel))
            if t is not None and rij is not None:
                for s in regel.segmenten:
                    if s.x0 < TABEL_X:
                        delen.append(tekst_div(s.tekst, s.x0, regel.top, regel.size, regel.vet, regel.kleur, False))
                for cel, tekst in cellen_per_tabel[t.index][rij.id].items():
                    x1 = t.cellen[cel][1] - _PAD
                    delen.append(tekst_div(tekst, x1, regel.top, regel.size, regel.vet, regel.kleur, True))
                continue
            if f"p{p.nr}l{i}" in tekst:                # aangepast via de chat: hele regel, links uitgelijnd
                if tekst[f"p{p.nr}l{i}"].strip():
                    delen.append(tekst_div(tekst[f"p{p.nr}l{i}"], regel.segmenten[0].x0, regel.top, regel.size,
                                           regel.vet, regel.kleur, False))
                continue
            vorige = ""
            for s in regel.segmenten:
                nieuw = _vervang_tekst(s.tekst, vorige, oud_jaar, datum)
                rechts = s.x0 >= TABEL_X and (is_bedrag(s.tekst) or bool(_JAAR_TOKEN.match(s.tekst)))
                delen.append(tekst_div(nieuw, s.x1 if rechts else s.x0, regel.top, regel.size, regel.vet,
                                       regel.kleur, rechts))
                vorige = s.tekst
        delen += [f'<a class="toc" href="#p{n}" style="left:{x0:.2f}pt;top:{top:.2f}pt;width:{x1 - x0:.2f}pt;'
                  f'height:{h:.2f}pt"></a>' for n, x0, top, x1, h in inhoud_links(rap, p)]
        if concept:
            delen.append('<div class="wm">CONCEPT</div>')
        delen.append("</section>")
    delen.append("</body></html>")
    return "\n".join(delen)


def render_pdf(rap: Rapport, uitkomsten: dict[str, Uitkomst], datum: str, concept: bool, uit_pdf: Path,
               tekst: dict[str, str] | None = None) -> Path:
    uit_pdf = Path(uit_pdf)
    uit_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        h = Path(tmp) / "rapport.html"
        h.write_text(render_html(rap, uitkomsten, datum, concept, tekst), encoding="utf-8")
        if uit_pdf.exists():
            uit_pdf.unlink()
        r = subprocess.run([chrome_pad(), *chrome_vlaggen(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                            f"--user-data-dir={Path(tmp) / 'profiel'}", "--no-first-run", "--no-default-browser-check",
                            f"--print-to-pdf={uit_pdf}", h.as_uri()], capture_output=True, text=True, timeout=180)
        if r.returncode != 0 or not uit_pdf.exists():
            raise RenderFout(f"PDF maken mislukt: {r.stderr[-500:]}")
    return uit_pdf


# --------------------------------------------------------------------------
# Controlerapport
# --------------------------------------------------------------------------
def schrijf_rapport(d: Doorrol, B: dict, pad: Path) -> Path:
    j, vj = d.jaar, d.jaar - 1
    r = [f"# Controlerapport jaarverslag {j} — {d.bedrijf}", "",
         f"Datum samenstelling {d.datum}. Bron: Asperion-export ({d.asperion_meta.get('exportdatum', '?')}) en "
         f"jaarverslag {vj}.", ""]
    if d.instructies.strip():
        r += ["Extra instructies van de samensteller (meegegeven aan Claude):", "",
              *[f"> {regel}" for regel in d.instructies.strip().splitlines()], ""]
    r += ["## 1. Controles", "", "| Status | Controle | Detail |", "|---|---|---|"]
    r += [f"| {k['status']} | {k['controle']} | {k['detail']} |" for k in d.controles]
    bron = "Claude Opus 5.5" if d.aandachtspunten_bron == "ai" else "code (zonder AI)"
    r += ["", f"## 2. Aandachtspunten ({bron})", ""]
    r += [f"- **[{a['ernst']}] {a['titel']}.** {a['toelichting']}" for a in d.aandachtspunten] or ["- geen"]
    r += ["", "## 3. Kerncijfers", "", f"| | {j} | {vj} |", "|---|---:|---:|"]
    r += [f"| {lab} | {fmt(n)} | {fmt(o)} |" for lab, n, o in d.kerncijfers]
    r += ["", f"## 4. Alle regels: herkomst en bewijs", "",
          f"Bewijs = de formule toegepast op de Asperion-kolom {vj}; moet gelijk zijn aan het oude rapport (marge {MARGE}).",
          "", f"| Pag. | Tabel | Regel | Rapport {vj} | Bewijs {vj} | Nieuw {j} | Door | Formule | Status |",
          "|---:|---|---|---:|---:|---:|---|---|---|"]
    for u in d.uitkomsten.values():
        lab = u.rij.label or f"*totaal {u.rij.groep}*"
        r.append(f"| {u.tabel.pagina} | {u.tabel.kop} | {lab} | {fmt(u.oud)} | "
                 f"{fmt(u.bewijs) if u.bewijs is not None else '—'} | {fmt(u.nieuw) if u.nieuw is not None else '?'} | "
                 f"{u.bron} | {formule_tekst(u.termen, B)} | {u.status}{(': ' + u.melding) if u.melding else ''} |")
    if d.aanpassingen:
        r += ["", "## Aanpassingen via de chat", ""]
        for n, a in enumerate(d.aanpassingen, 1):
            r += [f"{n}. **Vraag:** {a['vraag']}", f"   **Antwoord:** {a['antwoord']}"]
            r += [f"   - doorgevoerd: {w}" for w in a["doorgevoerd"]]
            r += [f"   - niet doorgevoerd: {w}" for w in a["geweigerd"]]
    if d.gebruik:
        r += ["", "## 5. AI-gebruik (Claude Opus 5.5)", "", "| Taak | Input | Output | Kosten |", "|---|---:|---:|---:|"]
        r += [f"| {g.taak} | {g.input_tokens} | {g.output_tokens} | ${g.kosten_usd:.4f} |" for g in d.gebruik]
        r.append(f"| **Totaal** | | | **${d.kosten_usd:.4f}** |")
    pad = Path(pad)
    pad.write_text("\n".join(r) + "\n", encoding="utf-8")
    return pad


def bewaar_koppelingen(d: Doorrol, pad: Path) -> Path:
    """Alle formules met herkomst en bewijs, machineleesbaar (controle en hergebruik)."""
    import json
    rijen = [{"id": i, "pagina": u.tabel.pagina, "tabel": u.tabel.kop, "regel": u.rij.label or f"totaal {u.rij.groep}",
              "oud": u.oud, "bewijs": u.bewijs, "nieuw": u.nieuw, "door": u.bron, "termen": u.termen,
              "toelichting": u.toelichting, "status": u.status, "melding": u.melding}
             for i, u in d.uitkomsten.items()]
    Path(pad).write_text(json.dumps({"jaar": d.jaar, "bedrijf": d.bedrijf, "regels": rijen,
                                     "tekstwijzigingen": d.tekstwijzigingen}, indent=1,
                                    ensure_ascii=False), encoding="utf-8")
    return Path(pad)


# --------------------------------------------------------------------------
# De ene stap
# --------------------------------------------------------------------------
def rol_door(oud_pdf: Path, balans: Path, datum: date, kantoor: dict, ai: ClaudeAI | None, uitmap: Path,
             voortgang=None, rapport: Rapport | None = None, instructies: str = "") -> Doorrol:
    stap = voortgang or (lambda s, d="": None)
    stap("balans", "")
    asp = lees_asperion(Path(balans))
    jaar = boekjaar(asp)
    stap("balans", f"{len(asp['rekeningen'])} grootboekrekeningen, boekjaar {jaar}")

    stap("rapport", "")
    rap = rapport or lees_rapport(Path(oud_pdf))
    if not rap.tabellen:
        raise DoorrolFout("In het oude jaarverslag zijn geen tabellen gevonden; is dit een rapport van het kantoor?")
    if asp["meta"].get("bedrijf") and not zelfde_bedrijf(rap.bedrijf, asp["meta"]["bedrijf"]):
        raise DoorrolFout(f"Het jaarverslag is van {rap.bedrijf}, maar de balans van {asp['meta']['bedrijf']}. "
                          "Upload de stukken van dezelfde klant.")
    if rap.verslagjaar != jaar - 1:
        raise DoorrolFout(f"Het oude jaarverslag is van {rap.verslagjaar}, maar de balans is van {jaar}. "
                          f"Upload het jaarverslag {jaar - 1}.")
    stap("rapport", f"{rap.bedrijf}: {len(rap.paginas)} pagina's, {len(rap.tabellen)} tabellen")

    start = len(ai.gebruik) if ai else 0
    uitkomsten = koppel(rap, asp, ai, stap, instructies)

    stap("aandachtspunten", "")
    ctrl = controles(rap, asp, uitkomsten, jaar)
    kern = kerncijfers(uitkomsten)
    sig = signalen(uitkomsten, ctrl, kern, kantoor)
    waarschuwingen, bron = [], "code"
    punten = [{"titel": s["titel"], "toelichting": s["tekst"], "ernst": s["ernst"]}
              for s in sorted(sig, key=lambda s: {"hoog": 0, "middel": 1, "laag": 2}[s["ernst"]])][:5]
    if ai is not None:
        try:
            punten = ai.aandachtspunten({"boekjaar": jaar,
                                         "kerncijfers": {lab: {"boekjaar": fmt(n), "vorig_jaar": fmt(o)}
                                                         for lab, n, o in kern.values()},
                                         "signalen": [{"ernst": s["ernst"], "titel": s["titel"], "tekst": s["tekst"]}
                                                      for s in sig]}, instructies)
            bron = "ai"
        except AIFout as e:
            waarschuwingen.append(f"Claude niet beschikbaar ({e}); aandachtspunten zonder AI opgesteld.")
    stap("aandachtspunten", f"{len(punten)} punten")

    stap("pdf", "")
    datum_s = datum_nl(datum)
    d = Doorrol(rapport=rap, jaar=jaar, datum=datum_s, bedrijf=rap.bedrijf, uitkomsten=uitkomsten, controles=ctrl,
                signalen=sig, aandachtspunten=punten, aandachtspunten_bron=bron,
                gebruik=list(ai.gebruik[start:]) if ai else [], kerncijfers=list(kern.values()),
                asperion_meta=asp["meta"], waarschuwingen=waarschuwingen, bronnen=bronnen(asp),
                instructies=instructies, asperion=asp, kantoor=kantoor)
    uitmap = Path(uitmap)
    titel = next((r.tekst.split()[0] for r in rap.paginas[0].regels if r.size >= 20), "Jaarrapport")
    naam = f"{rap.bedrijf} {titel} {jaar}"
    d.pdf = render_pdf(rap, uitkomsten, datum_s, True, uitmap / f"{naam} (concept).pdf")
    d.definitief = render_pdf(rap, uitkomsten, datum_s, False, uitmap / f"{naam}.pdf")
    d.rapport_md = schrijf_rapport(d, bronnen(asp), uitmap / f"controlerapport_{jaar}.md")
    bewaar_koppelingen(d, uitmap / "koppelingen.json")
    stap("pdf", d.pdf.name)
    return d


# --------------------------------------------------------------------------
# Aanpassen via de chat (na het maken)
# --------------------------------------------------------------------------
def _breedte(tekst: str, size: float, vet: bool) -> float:
    """Ruwe schatting van de tekstbreedte in pt (Helvetica: gemiddeld ~0,5 em per teken)."""
    return len(tekst) * size * (0.54 if vet else 0.5)


def _herhaald(regels: dict[str, tuple]) -> set[str]:
    """Kop- en voetregels die op (bijna) elke pagina terugkomen: niet naar Claude sturen."""
    from collections import Counter
    norm = {k: re.sub(r"\d+", "#", v[2]) for k, v in regels.items()}
    tel = Counter(norm.values())
    return {k for k, v in norm.items() if tel[v] >= 3}


def pas_aan(d: Doorrol, vraag: str, ai: ClaudeAI | None, gesprek: list[dict] | None = None) -> dict:
    """Eén chatbericht verwerken: Claude stelt formules en/of tekstregels voor, de code bewijst en controleert
    alles opnieuw, weigert wat niet klopt en maakt het rapport opnieuw op. Geeft het logboekitem terug."""
    if ai is None:
        raise DoorrolFout("Voor aanpassen is Claude nodig (vul de API-sleutel in).")
    if not d.asperion:
        raise DoorrolFout("Dit jaarverslag is gemaakt met een oudere versie; maak het opnieuw om aan te passen.")
    B, uk, rap = d.bronnen, d.uitkomsten, d.rapport
    regels_tekst = tekstregels(rap, d.datum)
    herhaald = _herhaald(regels_tekst)
    nu = {k: d.tekstwijzigingen.get(k, v[2]) for k, v in regels_tekst.items()}

    regels = [{"id": i, "pagina": u.tabel.pagina, "tabel": u.tabel.kop,
               "omschrijving": u.rij.label or f"totaal {u.rij.groep}".strip(), "oud": u.oud, "nieuw": u.nieuw,
               "formule": [[t["bron"], t["naam"], int(t["teken"])] for t in u.termen], "status": u.status}
              for i, u in uk.items()]
    asperion = [{"sleutel": k, "omschrijving": v["omschrijving"], "vorig": v["vorig"], "huidig": v["huidig"]}
                for k, v in B.items()]
    teksten = [[k, v[0], nu[k]] for k, v in regels_tekst.items() if k not in herhaald and nu[k].strip()]

    start = len(ai.gebruik)
    voorstel = ai.pas_aan(vraag, regels, asperion, teksten, gesprek, d.instructies)
    d.gebruik += ai.gebruik[start:]
    doorgevoerd, geweigerd, paginas = [], [], set()

    def naam(u: Uitkomst) -> str:
        return f"{u.rij.label or ('totaal ' + u.rij.groep).strip()} (p. {u.tabel.pagina})"

    # 1. Formules: toepassen, bewijzen op vorig jaar, wat niet klopt terugdraaien
    oud_stand = {i: (u.termen, u.bron, u.toelichting, u.nieuw) for i, u in uk.items()}
    gewijzigd = []
    for f in voorstel["formules"]:
        if f["rij"] not in uk:
            geweigerd.append(f"onbekende regel {f['rij']}")
            continue
        u = uk[f["rij"]]
        u.termen = [{"bron": t["bron"], "naam": t["naam"], "teken": int(t["teken"])} for t in f["termen"]]
        u.bron, u.toelichting, u.melding = "chat", f["toelichting"], ""
        gewijzigd.append(f["rij"])
    if gewijzigd:
        _bewijs(uk, B)
        for i in gewijzigd:
            u = uk[i]
            if u.bewijs is not None and u.oud != 0 and not _klopt(u.bewijs, u.oud) and _klopt(-u.bewijs, u.oud):
                u.termen = [{**t, "teken": -int(t["teken"])} for t in u.termen]
        _bewijs(uk, B)
        for u in uk.values():
            _status(u)
        terug = [i for i in gewijzigd if uk[i].status == FOUT]
        for i in terug:
            u = uk[i]
            geweigerd.append(f"{naam(u)}: {u.melding or 'klopt niet op vorig jaar'}")
            u.termen, u.bron, u.toelichting, _ = oud_stand[i]
            u.melding = ""
        if terug:
            _bewijs(uk, B)
            for u in uk.values():
                _status(u)
        for i, u in uk.items():                  # ook regels die via 'rij'-termen meeveranderen
            if u.nieuw != oud_stand[i][3]:
                paginas.add(u.tabel.pagina)
                doorgevoerd.append(f"{naam(u)}: {_cel_tekst(oud_stand[i][3])} → {_cel_tekst(u.nieuw)}")
        for i in gewijzigd:
            if i not in terug and uk[i].nieuw == oud_stand[i][3]:
                doorgevoerd.append(f"{naam(uk[i])}: formule aangepast, bedrag gelijk")
                paginas.add(uk[i].tabel.pagina)

    # 2. Tekstregels: alleen bestaande regels, passend op de regel, geen verzonnen bedragen
    from .ai import bedragen_in_tekst
    bekend = {fmt(abs(v)) for u in uk.values() for v in (u.oud, u.nieuw) if v is not None}
    bekend |= {b for v in nu.values() for b in bedragen_in_tekst(v)}
    for t in voorstel["teksten"]:
        k, tekst = t["regel"], t["tekst"].strip()
        if k not in regels_tekst:
            geweigerd.append(f"onbekende tekstregel {k}")
            continue
        pag, regel, origineel = regels_tekst[k]
        p = next(pg for pg in rap.paginas if pg.nr == pag)
        x0 = regel.segmenten[0].x0
        ruimte = max(p.breedte - 42 - x0, _breedte(origineel, regel.size, regel.vet) * 1.05)
        if _breedte(tekst, regel.size, regel.vet) > ruimte:
            geweigerd.append(f'p. {pag}: "{tekst[:60]}" past niet op de regel; maak de tekst korter')
            continue
        vreemd = sorted(bedragen_in_tekst(tekst) - bekend)
        if vreemd:
            geweigerd.append(f"p. {pag}: bedrag {', '.join(vreemd)} komt niet uit de cijfers")
            continue
        if tekst == origineel:
            d.tekstwijzigingen.pop(k, None)
        else:
            d.tekstwijzigingen[k] = tekst
        paginas.add(pag)
        doorgevoerd.append(f'p. {pag}: "{nu[k][:50]}" → "{tekst[:50] or "(weggehaald)"}"')

    item = {"vraag": vraag, "antwoord": voorstel["antwoord"], "doorgevoerd": doorgevoerd, "geweigerd": geweigerd,
            "controles": [], "paginas": sorted(paginas)}
    if doorgevoerd:
        voor = {k["controle"]: k["status"] for k in d.controles}
        d.controles = controles(rap, d.asperion, uk, d.jaar)
        kern = kerncijfers(uk)
        d.kerncijfers = list(kern.values())
        d.signalen = signalen(uk, d.controles, kern, d.kantoor)
        rang = {OK: 0, LET_OP: 1, FOUT: 2}
        item["controles"] = [f"{k['controle']}: {k['status']}" for k in d.controles
                             if rang[k["status"]] > rang.get(voor.get(k["controle"], OK), 0)]
        d.pdf = render_pdf(rap, uk, d.datum, True, d.pdf, d.tekstwijzigingen)
        d.definitief = render_pdf(rap, uk, d.datum, False, d.definitief, d.tekstwijzigingen)
    d.aanpassingen.append(item)
    d.rapport_md = schrijf_rapport(d, B, d.rapport_md)
    bewaar_koppelingen(d, d.pdf.parent / "koppelingen.json")
    return item
