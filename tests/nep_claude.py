"""Nagebootste Claude-client voor tests en demo's zonder API-sleutel (kost niets).

Herkent de taak aan het JSON-schema en geeft een plausibel antwoord terug.
Tokens worden geschat als tekens/4 (alleen indicatief).
Gebruik in de app: JAARBOEK_NEP_AI=1 streamlit run app.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

DATA = Path(__file__).resolve().parent / "data"


def _antwoord(data: dict, invoer: str, stop_reason: str = "end_turn"):
    tekst = json.dumps(data, ensure_ascii=False)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=tekst)], stop_reason=stop_reason, model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=len(invoer) // 4, output_tokens=len(tekst) // 4 + 200,
                              cache_read_input_tokens=0, cache_creation_input_tokens=0))


class _Messages:
    def __init__(self, ouder):
        self.ouder = ouder

    def create(self, **kw):
        self.ouder.verzoeken.append(kw)
        if self.ouder.weiger:
            return _antwoord({}, "", stop_reason="refusal")
        schema = kw["output_config"]["format"]["schema"]["properties"]
        invoer = kw["system"] + kw["messages"][0]["content"]
        if "antwoord" in schema:
            inv = json.loads(kw["messages"][0]["content"])
            a = self.ouder.aanpassing
            if a is not None:
                return _antwoord(a(inv) if callable(a) else a, invoer)
            return _antwoord(_nep_aanpassing(inv), invoer)
        if "aandachtspunten" in schema:
            s = json.loads(kw["messages"][0]["content"])
            volgorde = {"hoog": 0, "middel": 1, "laag": 2}
            punten = [{"titel": x["titel"], "toelichting": x["tekst"], "ernst": x["ernst"]}
                      for x in sorted(s["signalen"], key=lambda x: volgorde[x["ernst"]])[:4]]
            if self.ouder.verzin_bedrag and punten:
                punten[0]["toelichting"] += " Mogelijk ontbreekt 12.345 aan kosten."
            return _antwoord({"aandachtspunten": punten}, invoer)
        if "voorstellen" in schema:
            s = json.loads(kw["messages"][0]["content"])
            opties = schema["voorstellen"]["items"]["properties"]["post"]["enum"]
            labels = s["opties"]

            def woorden(t):
                return {w for w in re.findall(r"[a-z]{3,}", t.lower())}

            voorstellen = []
            for r in s["nieuwe_rekeningen"]:
                beste = max(opties, key=lambda o: len(woorden(r["naam"]) & woorden(labels.get(o, o) + " " + o)))
                post = self.ouder.koppel.get(r["nr"]) or beste
                voorstellen.append({"rekening": r["nr"], "post": post, "zekerheid": "hoog",
                                    "toelichting": f"Naam '{r['naam']}' past bij deze post."})
            return _antwoord({"voorstellen": voorstellen}, invoer)
        if "formules" in schema:
            inv = json.loads(kw["messages"][0]["content"])
            if self.ouder.opname:                     # echte Claude-formules afspelen
                gevraagd = {r["id"] for r in inv["regels"]}
                formules = [{"rij": r["id"], "termen": r["termen"], "toelichting": r["toelichting"]}
                            for r in json.loads(Path(self.ouder.opname).read_text(encoding="utf-8"))["regels"]
                            if r["id"] in gevraagd and r["door"] == "claude"]
                return _antwoord({"formules": formules}, invoer)
            return _antwoord({"formules": _zoek_formules(inv)}, invoer)
        if "posten" in schema:
            bron = self.ouder.uitlezing or (DATA / "misto_2024_uitlezing.json")
            return _antwoord(json.loads(Path(bron).read_text(encoding="utf-8")), invoer)
        raise ValueError("onbekende taak")


def _zoek_formules(inv: dict) -> list[dict]:
    """Test-dubbel voor 'regels koppelen': zoekt systematisch een formule die het bewijs haalt
    (1 of 2 Asperion-bedragen, of een som van 2-3 andere regels)."""
    from itertools import combinations, product
    bronnen = [(b["sleutel"], b["vorig"]) for b in inv["asperion"]]
    regels = {r["id"]: r["waarde_verslagjaar"] for r in inv["al_bekende_regels"] + inv["regels"]}

    def term(sleutel, s):
        bron, _, naam = sleutel.partition(":")
        return {"bron": bron, "naam": naam, "teken": s}

    uit = []
    for r in inv["regels"]:
        doel, gevonden = r["waarde_verslagjaar"], None
        if doel == 0:
            gevonden = []
        for (k, v), s in product(bronnen, (1, -1)):
            if gevonden is None and abs(s * v - doel) <= 2:
                gevonden = [term(k, s)]
        if gevonden is None:
            for ((k1, v1), (k2, v2)), (s1, s2) in product(combinations(bronnen, 2), product((1, -1), repeat=2)):
                if abs(s1 * v1 + s2 * v2 - doel) <= 2:
                    gevonden = [term(k1, s1), term(k2, s2)]
                    break
        if gevonden is None:
            andere = [(i, w) for i, w in regels.items() if i != r["id"] and w != 0]
            for n in (2, 3):
                for combi in combinations(andere, n):
                    if abs(sum(w for _, w in combi) - doel) <= 2:
                        gevonden = [{"bron": "rij", "naam": i, "teken": 1} for i, _ in combi]
                        break
                if gevonden is not None:
                    break
        if gevonden is not None:
            uit.append({"rij": r["id"], "termen": gevonden, "toelichting": "nagebootst"})
    return uit


def _nep_aanpassing(inv: dict) -> dict:
    """Demo zonder API: begrijpt alleen 'vervang "X" door "Y"' (tekst)."""
    m = re.search(r'vervang\s+["\'“](.+?)["\'”]\s+door\s+["\'“](.*?)["\'”]', inv["vraag"], re.I)
    if m:
        oud, nieuw = m.group(1), m.group(2)
        for rid, _pag, tekst in inv["tekstregels"]:
            if oud in tekst:
                return {"antwoord": f"Ik vervang '{oud}' door '{nieuw}'.", "formules": [],
                        "teksten": [{"regel": rid, "tekst": tekst.replace(oud, nieuw)}]}
        return {"antwoord": f"Ik vind '{oud}' niet in de tekst.", "formules": [], "teksten": []}
    return {"antwoord": "Demo-modus: ik begrijp alleen 'vervang \"oude tekst\" door \"nieuwe tekst\"'.",
            "formules": [], "teksten": []}


class NepClaude:
    def __init__(self, koppel: dict | None = None, weiger: bool = False, verzin_bedrag: bool = False,
                 uitlezing: Path | None = None, opname: Path | None = None, aanpassing=None):
        self.uitlezing = uitlezing   # ander 'uitgelezen jaarverslag' (JSON) dan het MISTO-verslag 2024
        self.opname = opname         # koppelingen.json van een echte Claude-run: formules afspelen
        self.verzoeken: list[dict] = []
        self.koppel = koppel or {}
        self.weiger = weiger
        self.verzin_bedrag = verzin_bedrag
        self.aanpassing = aanpassing  # vast antwoord (dict) of functie(invoer) -> dict voor de chat-aanpassing
        self.beta = SimpleNamespace(messages=_Messages(self))
