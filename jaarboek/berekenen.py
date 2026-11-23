"""Jaarrekening berekenen uit de ingelezen Asperion-gegevens.

Regel: elk bedrag dat Asperion toont wordt letterlijk overgenomen (rekeningen en
(sub)totalen). Alleen wat Asperion niet toont wordt opgeteld: brutomarge,
bedrijfsresultaat, vlottende activa, balanstotalen, eigen vermogen en de splitsing
van leaseverplichtingen in lang en kort. Er wordt niets gecorrigeerd.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .structuur import KOSTEN_POSTEN, WV_STRUCTUUR, credit_posten, fmt

OK, LET_OP, FOUT = "OK", "LET OP", "FOUT"


class KoppelFout(Exception):
    """Er zijn grootboekrekeningen die (nog) niet aan een post gekoppeld zijn."""

    def __init__(self, rekeningen: list[dict]):
        self.rekeningen = rekeningen
        lijst = ", ".join(f"{r['nr']} {r['naam']}" for r in rekeningen)
        super().__init__(f"Niet-gekoppelde grootboekrekeningen: {lijst}")


@dataclass
class Berekening:
    c: dict                      # posten huidig jaar
    controles: list[dict]        # {"status", "controle", "detail"}
    lease: dict                  # per contract: saldo's en splitsing
    ev_verloop: dict
    aansluiting: list[dict]      # Asperion vorig jaar vs gepubliceerde jaarrekening
    ontbrekend: list[str] = field(default_factory=list)


def niet_gekoppeld(asp: dict, mapping: dict) -> list[dict]:
    return [{"nr": nr, **r} for nr, r in asp["rekeningen"].items() if nr not in mapping["rekeningen"]]


def bereken(asp: dict, mapping: dict, vj: dict) -> Berekening:
    rek_map = mapping["rekeningen"]
    nieuw = niet_gekoppeld(asp, mapping)
    if nieuw:
        raise KoppelFout(nieuw)

    credit = credit_posten(mapping)
    blad = {"huidig": defaultdict(int), "vorig": defaultdict(int)}
    for nr, r in asp["rekeningen"].items():
        post = rek_map[nr]
        teken = -1 if post in credit else 1
        blad["huidig"][post] += teken * r["huidig"]
        blad["vorig"][post] += teken * r["vorig"]
    L, LV = blad["huidig"], blad["vorig"]
    ontbrekend: list[str] = []

    def asp_waarde(deel: str, soort: str, naam: str, kolom: str) -> int:
        bron = asp[soort].get((deel, naam))
        if bron is None:
            if f"{deel}:{naam}" not in ontbrekend:
                ontbrekend.append(f"{deel}:{naam}")
            return 0
        return bron[kolom]

    c: dict[str, int] = {}

    # --- Winst-en-verliesrekening ----------------------------------------
    doelen = mapping["wv_doelen"]

    def doel(post: str, kolom: str) -> int:
        spec = doelen[post]
        if spec.get("bron") == "rekeningen":
            return sum(blad[kolom][k] for k in WV_STRUCTUUR[post])
        tot = sum(asp_waarde("wv", "groepen", g, kolom) for g in spec.get("groepen", []))
        tot += sum(asp_waarde("wv", "secties", s, kolom) for s in spec.get("secties", []))
        return -tot if post in ("omzet", "rentebaten") else tot  # opbrengsten staan credit

    for post, regels in WV_STRUCTUUR.items():
        c[post] = doel(post, "huidig")
        if len(regels) == 1:
            c[regels[0]] = c[post]           # regel = hele Asperion-groep
        else:
            for k in regels:
                c[k] = L[k]                  # getoonde rekeningbedragen

    c["afschr_mva"] = c["afschrijvingen"]
    c["totaal_kosten"] = sum(c[p] for p in KOSTEN_POSTEN)
    c["financieel"] = c["rentebaten"] - c["rentelasten"]
    c["brutomarge"] = c["omzet"] - c["kostprijs"]
    c["bedrijfsresultaat"] = c["brutomarge"] - c["totaal_kosten"]
    c["resultaat"] = asp["resultaat"]["wv"]["huidig"]

    # --- Balans -------------------------------------------------------------
    weergave = mapping["balans_weergave"]

    def getoond(post: str, kolom: str = "huidig") -> int:
        spec = weergave[post]
        v = (asp_waarde("balans", "groepen", spec["groep"], kolom) if "groep" in spec
             else asp_waarde("balans", "secties", spec["sectie"], kolom))
        return -v if spec.get("credit") else v

    c["mva_inventaris"] = getoond("mva_inventaris")
    c["mva_vervoer"] = getoond("mva_vervoer")
    c["mva"] = getoond("mva")
    c["debiteuren"] = c["deb_debiteuren"] = getoond("debiteuren")
    c["overige_vorderingen"] = c["ov_vooruitbetalingen"] = getoond("overige_vorderingen")
    c["vorderingen"] = getoond("vorderingen")
    c["bank_rc"] = L["bank_rc"]
    c["bank_spaar"] = L["bank_spaar"]
    c["tegoeden"] = c["bank_rc"] + c["bank_spaar"]
    c["krp_stortingen"] = c["kruisposten"] = L["krp_stortingen"]
    c["liquide"] = getoond("liquide")
    c["vlottend"] = c["vorderingen"] + c["liquide"]
    c["activa"] = c["mva"] + c["vlottend"]

    ev_rek = {nr: r for nr, r in asp["rekeningen"].items() if rek_map[nr] == "ev_rekeningen"}
    begin = -sum(r["huidig"] for r in ev_rek.values() if r["naam"].lower().startswith("beginkapitaal"))
    prive_rek = [r for r in ev_rek.values() if not r["naam"].lower().startswith(("beginkapitaal", "resultaat"))]
    prive = sum(r["huidig"] for r in prive_rek)
    c["kapitaal"] = c["ev"] = c["resultaat"] - getoond("kapitaal_rubriek")

    # Leaseverplichtingen: kortlopend deel = aflossing in het boekjaar (methode vorige jaarrekening)
    lease, kt_lease = {}, 0
    for post, spec in mapping.get("leasecontracten", {}).items():
        eind, begin_saldo = L[post], LV[post]
        aflossing = max(0, begin_saldo - eind)
        kort = min(aflossing, eind)
        c[spec["lt_post"]] = eind - kort
        kt_lease += kort
        lease[spec["label"]] = {"saldo_begin": begin_saldo, "saldo_eind": eind, "aflossing_boekjaar": aflossing,
                                "kortlopend": kort, "langlopend": eind - kort}
    c["kt_fin_lease"] = kt_lease
    c["lt"] = c["lt_fin_lease"] = getoond("lt_rubriek") - kt_lease
    c["crediteuren"] = getoond("crediteuren")
    c["belastingen"] = c["btw_omzetbelasting"] = getoond("belastingen")
    c["otb_nog_te_betalen"] = L["otb_nog_te_betalen"]
    c["otb_vooruitontvangen"] = L["otb_vooruitontvangen"]
    c["overlopende"] = getoond("overlopende")
    c["kt"] = c["kt_fin_lease"] + getoond("kt_rubriek")
    c["passiva"] = c["ev"] + c["lt"] + c["kt"]

    # --- Controles ---------------------------------------------------------
    vp = vj["posten"]
    ctrl: list[dict] = []

    def controle(status, tekst, detail, kort=None):
        ctrl.append({"status": status, "controle": tekst, "detail": detail, **({"kort": kort} if kort else {})})

    controle(OK, "Alle grootboekrekeningen uit de export zijn gekoppeld aan een post",
             f"{len(asp['rekeningen'])} rekeningen")

    def som(termen, bron):
        tot = 0
        for t in termen:
            neg = t.startswith("-")
            t = t.lstrip("-")
            if bron == "jr":
                v = c[t]
            else:
                deel, naam = t.split(":", 1)
                v = asp_waarde(deel, "secties", naam, "huidig")
            tot += -v if neg else v
        return tot

    afwijkend = []
    for r in mapping.get("rubriekcontrole", []):
        jr, a = som(r["jaarrekening"], "jr"), som(r["asperion"], "asp")
        if jr != a:
            afwijkend.append(f"{r['label']} (jaarrekening {fmt(jr)}, Asperion {fmt(a)})")
    controle(FOUT if afwijkend else OK, "Bedragen in de jaarrekening = bedragen zoals getoond in Asperion",
             f"{len(mapping.get('rubriekcontrole', []))} rubriektotalen vergeleken"
             + (f"; AFWIJKEND: {'; '.join(afwijkend)}" if afwijkend else ""))

    res_ok = c["resultaat"] == asp["resultaat"]["wv"]["huidig"] == asp["resultaat"]["balans"]["huidig"]
    controle(OK if res_ok else FOUT, "Resultaat W&V = resultaat volgens Asperion (W&V én balans)",
             f"jaarrekening {fmt(c['resultaat'])}, Asperion W&V {fmt(asp['resultaat']['wv']['huidig'])}, "
             f"Asperion balans {fmt(asp['resultaat']['balans']['huidig'])}")

    if "ev" in vp:
        controle(OK if begin == vp["ev"] else FOUT,
                 "Beginkapitaal boekjaar = eigen vermogen ultimo vorig jaar (jaarrekening vorig jaar)",
                 f"rekening beginkapitaal {fmt(begin)}, eigen vermogen jaarrekening {vj['jaar']} {fmt(vp['ev'])}")
    ev_eind = begin + c["resultaat"] - prive
    ev_verloop = {"begin": begin, "resultaat": c["resultaat"], "prive": -prive, "eind": ev_eind,
                  "prive_detail": {r["naam"]: -r["huidig"] for r in prive_rek}}
    controle(OK if ev_eind == c["ev"] else FOUT, "Eigen-vermogensverloop sluit (begin + resultaat - privé = eind)",
             f"{fmt(begin)} + {fmt(c['resultaat'])} - {fmt(prive)} = {fmt(ev_eind)} (balans: {fmt(c['ev'])})")

    verschil = c["activa"] - c["passiva"]
    controle(LET_OP if verschil else OK, "Balanstotaal activa en passiva (getallen ongewijzigd uit Asperion)",
             f"activa {fmt(c['activa'])} / passiva {fmt(c['passiva'])}"
             + (f"; verschil {fmt(verschil)} door afronding in Asperion, niet aangepast" if verschil else ""),
             kort=(f"Balans wijkt € {fmt(abs(verschil))} af", "Afronding in Asperion; cijfers niet aangepast."))
    wv_som = c["bedrijfsresultaat"] + c["financieel"]
    controle(LET_OP if wv_som != c["resultaat"] else OK,
             "Bedrijfsresultaat + financiële baten en lasten = resultaat Asperion",
             f"{fmt(c['bedrijfsresultaat'])} {'+' if c['financieel'] >= 0 else '-'} {fmt(abs(c['financieel']))} = "
             f"{fmt(wv_som)}, Asperion-resultaat {fmt(c['resultaat'])}"
             + ("; verschil door afronding in Asperion, niet aangepast" if wv_som != c["resultaat"] else ""),
             kort=(f"Resultaat wijkt € {fmt(abs(wv_som - c['resultaat']))} af", "Afronding in Asperion; cijfers niet aangepast."))
    controle(OK if all(t["debet"] == t["credit"] for t in asp["totaal"].values()) else FOUT,
             "Totaal-debet = totaal-credit in de Asperion-export (balans en W&V)",
             "; ".join(f"{k}: {fmt(t['debet'])} / {fmt(t['credit'])}" for k, t in asp["totaal"].items()))

    # Aansluiting: Asperion-kolom vorig jaar vs gepubliceerde jaarrekening vorig jaar
    vergelijk: dict[str, tuple[int, int]] = {}
    for post in weergave:
        if post == "kapitaal_rubriek":
            if "kapitaal" in vp:
                vergelijk["kapitaal"] = (vp["kapitaal"], asp["resultaat"]["balans"]["vorig"] - getoond(post, "vorig"))
        elif post == "lt_rubriek":
            if "lt" in vp:
                vergelijk["leaseverplichtingen (lang + kort)"] = (vp["lt"] + vp.get("kt_fin_lease", 0), getoond(post, "vorig"))
        elif post == "kt_rubriek":
            if "kt" in vp:
                vergelijk["kortlopende schulden excl. lease"] = (vp["kt"] - vp.get("kt_fin_lease", 0), getoond(post, "vorig"))
        elif post in vp:
            vergelijk[post] = (vp[post], getoond(post, "vorig"))
    for post in ("bank_rc", "bank_spaar", "krp_stortingen"):
        if post in vp:
            vergelijk[post] = (vp[post], LV[post])
    for post in WV_STRUCTUUR:
        if post in vp:
            vergelijk[post] = (vp[post], doel(post, "vorig"))
    if "resultaat" in vp:
        vergelijk["resultaat"] = (vp["resultaat"], asp["resultaat"]["wv"]["vorig"])
    aansluiting = [{"post": k, "jaarrekening": jr, "asperion": a, "verschil": a - jr}
                   for k, (jr, a) in vergelijk.items()]
    max_afw = max((abs(a["verschil"]) for a in aansluiting), default=0)
    groot = [a["post"] for a in aansluiting if abs(a["verschil"]) > 2]
    controle(OK if not groot else FOUT,
             f"Asperion-kolom {vj['jaar']} sluit aan op de jaarrekening {vj['jaar']}",
             f"{len(aansluiting)} posten vergeleken, grootste verschil {max_afw} euro"
             + (f"; AFWIJKEND: {', '.join(groot)} (controleer de koppelingen of het dossier)" if groot else " (afronding)"))

    if ontbrekend:
        controle(LET_OP, "Rubrieken uit de koppeling gevonden in Asperion",
                 f"niet gevonden (als 0 meegenomen): {', '.join(ontbrekend)}",
                 kort=("Rubriek niet gevonden", "Als 0 meegenomen; controleer de koppeling."))

    return Berekening(c=c, controles=ctrl, lease=lease, ev_verloop=ev_verloop,
                      aansluiting=aansluiting, ontbrekend=ontbrekend)
