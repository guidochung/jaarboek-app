"""Claude-aanroepen (model: Claude Opus 5.5). Zo zuinig mogelijk:

- Claude krijgt nooit de volledige balans of een PDF, alleen compacte JSON/tekst.
- Gestructureerde uitvoer (JSON-schema): korte antwoorden, niets te parsen.
- Effort 'medium' als standaard (instelbaar in kantoor.json).
- Bedragen in AI-tekst worden door code gecontroleerd tegen de invoer.

Ook staat 'server-side fallback' aan: als Claude Opus 5.5 een verzoek om
veiligheidsredenen weigert, probeert de API automatisch een ander model.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime

MODEL = "claude-opus-5-5"
# Prijzen Claude Opus 5.5 in dollar per miljoen tokens
PRIJZEN = {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AIFout(Exception):
    pass


@dataclass
class Gebruik:
    taak: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int
    kosten_usd: float
    model: str
    tijd: str

    def als_dict(self) -> dict:
        return asdict(self)


def kosten(input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    return (input_tokens * PRIJZEN["input"] + output_tokens * PRIJZEN["output"]
            + cache_read * PRIJZEN["cache_read"] + cache_write * PRIJZEN["cache_write"]) / 1_000_000


EXTRA_REGEL = ("Onder 'extra_instructies' staan aanwijzingen van de samensteller voor deze klant. Volg ze waar ze "
               "passen, maar verzin nooit bedragen en wijk niet af van de regels hierboven.")


class ClaudeAI:
    def __init__(self, api_key: str | None = None, client=None, effort: str = "medium", fallbacks: bool = True):
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.client = client
        self.effort = effort
        self.fallbacks = fallbacks
        self.gebruik: list[Gebruik] = []

    # ------------------------------------------------------------------
    def _vraag(self, taak: str, system: str, inhoud: str, schema: dict, max_tokens: int,
               effort: str | None = None) -> dict:
        kwargs = dict(
            model=MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": inhoud}],
            output_config={"effort": effort or self.effort,
                           "format": {"type": "json_schema", "schema": schema}},
        )
        if self.fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["extra_body"] = {"fallbacks": "default"}
        try:
            antwoord = self.client.beta.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001 - vertaal SDK-fouten naar begrijpelijke meldingen
            naam = type(e).__name__
            if self.fallbacks and naam == "BadRequestError" and "fallback" in str(e).lower():
                self.fallbacks = False  # fallback niet beschikbaar voor dit account: zonder opnieuw
                return self._vraag(taak, system, inhoud, schema, max_tokens, effort)
            meldingen = {
                "AuthenticationError": "De Claude API-sleutel is ongeldig.",
                "PermissionDeniedError": "Deze API-sleutel heeft geen toegang tot Claude Opus 5.5.",
                "RateLimitError": "Te veel verzoeken of tegoed op; probeer het later opnieuw.",
                "APIConnectionError": "Geen verbinding met de Claude API (internet?).",
                "APITimeoutError": "De Claude API reageerde niet op tijd.",
            }
            raise AIFout(meldingen.get(naam, f"Claude API-fout ({naam}): {e}")) from e

        u = antwoord.usage
        g = Gebruik(taak=taak, input_tokens=u.input_tokens or 0, output_tokens=u.output_tokens or 0,
                    cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                    cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
                    kosten_usd=0.0, model=getattr(antwoord, "model", MODEL),
                    tijd=datetime.now().isoformat(timespec="seconds"))
        g.kosten_usd = round(kosten(g.input_tokens, g.output_tokens, g.cache_read, g.cache_write), 5)
        self.gebruik.append(g)

        if antwoord.stop_reason == "refusal":
            raise AIFout("Claude heeft dit verzoek geweigerd.")
        if antwoord.stop_reason == "max_tokens":
            raise AIFout("Het antwoord van Claude werd afgekapt (max_tokens).")
        tekst = next((b.text for b in antwoord.content if getattr(b, "type", "") == "text"), None)
        if tekst is None:
            raise AIFout("Claude gaf geen antwoord terug.")
        try:
            return json.loads(tekst)
        except json.JSONDecodeError as e:
            raise AIFout("Het antwoord van Claude was geen geldige JSON.") from e

    # ------------------------------------------------------------------
    def aandachtspunten(self, samenvatting: dict, instructies: str = "") -> list[dict]:
        schema = {
            "type": "object",
            "properties": {"aandachtspunten": {"type": "array", "items": {
                "type": "object",
                "properties": {"titel": {"type": "string"}, "toelichting": {"type": "string"},
                               "ernst": {"type": "string", "enum": ["hoog", "middel", "laag"]}},
                "required": ["titel", "toelichting", "ernst"], "additionalProperties": False}}},
            "required": ["aandachtspunten"], "additionalProperties": False,
        }
        system = (
            "Je bent samensteller bij een Nederlands administratiekantoor en beoordeelt een concept-jaarrekening. "
            "Je krijgt kerncijfers en door code berekende signalen. Geef maximaal 4 aandachtspunten: alleen wat de "
            "accountant echt moet nakijken of bij de klant moet navragen. Laat afrondingsverschillen en puur "
            "informatieve mutaties weg tenzij ze iets betekenen. Combineer signalen die bij elkaar horen.\n"
            "Vorm per punt:\n"
            "- titel: maximaal 5 woorden, zonder punt (bijv. 'Overlopende passiva nihil').\n"
            "- toelichting: één zin van maximaal 18 woorden die zegt wat te doen, met hooguit één of twee bedragen.\n"
            "Gebruik alleen bedragen die letterlijk in de invoer staan; reken niets zelf uit. Geen inleiding, "
            "geen herhaling van de titel, geen woorden als 'cruciaal' of 'belangrijk om op te merken'.\n"
            "Ernst: hoog = kan de jaarrekening onjuist maken; middel = navragen bij de klant; laag = ter info."
        )
        if instructies.strip():
            samenvatting = {**samenvatting, "extra_instructies": instructies.strip()}
            system += "\n" + EXTRA_REGEL
        data = self._vraag("aandachtspunten", system, json.dumps(samenvatting, ensure_ascii=False, separators=(",", ":")),
                           schema, max_tokens=3000)
        punten = data.get("aandachtspunten", [])[:4]
        toegestaan = toegestane_bedragen(samenvatting)
        for p in punten:
            onbekend = sorted(b for b in bedragen_in_tekst(p["titel"] + " " + p["toelichting"]) if b not in toegestaan)
            p["onbekende_bedragen"] = onbekend
        return punten

    # ------------------------------------------------------------------
    def koppelvoorstellen(self, nieuwe: list[dict], opties: dict[str, str], voorbeelden: dict[str, list]) -> list[dict]:
        """nieuwe: [{nr, naam, deel, sectie, groep, huidig}]; opties: post -> omschrijving."""
        schema = {
            "type": "object",
            "properties": {"voorstellen": {"type": "array", "items": {
                "type": "object",
                "properties": {"rekening": {"type": "string", "enum": [r["nr"] for r in nieuwe]},
                               "post": {"type": "string", "enum": sorted(opties)},
                               "zekerheid": {"type": "string", "enum": ["hoog", "middel", "laag"]},
                               "toelichting": {"type": "string"}},
                "required": ["rekening", "post", "zekerheid", "toelichting"], "additionalProperties": False}}},
            "required": ["voorstellen"], "additionalProperties": False,
        }
        system = (
            "Je koppelt nieuwe grootboekrekeningen uit Asperion aan posten van een Nederlandse jaarrekening. "
            "Kies per rekening precies één post uit de opties. Gebruik de rekeningnaam, de Asperion-groep en de "
            "voorbeelden van bestaande rekeningen in dezelfde groep. Toelichting: één korte zin."
        )
        invoer = {
            "nieuwe_rekeningen": [{k: r.get(k) for k in ("nr", "naam", "deel", "sectie", "groep", "huidig")} for r in nieuwe],
            "opties": opties,
            "bestaande_rekeningen_in_dezelfde_groep": voorbeelden,
        }
        data = self._vraag("koppelvoorstellen", system, json.dumps(invoer, ensure_ascii=False, separators=(",", ":")),
                           schema, max_tokens=4000)
        geldig = {r["nr"] for r in nieuwe}
        return [v for v in data.get("voorstellen", []) if v["rekening"] in geldig and v["post"] in opties]

    # ------------------------------------------------------------------
    def lees_jaarrekening(self, tekst: str, posten: dict[str, str]) -> dict:
        """Eenmalig per nieuwe klant: tekst van de vorige jaarrekening -> gestructureerd dossier."""
        schema = {
            "type": "object",
            "properties": {
                "bedrijfsnaam": {"type": "string"},
                "jaren": {"type": "array", "items": {"type": "integer"}},
                "vestigingsplaats": {"type": "string"},
                "kvk": {"type": "string"},
                "posten": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"post": {"type": "string", "enum": sorted(posten)},
                                   "bedragen": {"type": "array", "items": {"type": "integer"}}},
                    "required": ["post", "bedragen"], "additionalProperties": False}},
                "leasecontracten": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"naam": {"type": "string"},
                                   "langlopend": {"type": "array", "items": {"type": "integer"}},
                                   "tekst": {"type": "string"}},
                    "required": ["naam", "langlopend", "tekst"], "additionalProperties": False}},
                "afschrijvingen_tekst": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["bedrijfsnaam", "jaren", "vestigingsplaats", "kvk", "posten", "leasecontracten",
                         "afschrijvingen_tekst"],
            "additionalProperties": False,
        }
        system = (
            "Je leest een Nederlandse jaarrekening (platte tekst) uit naar gestructureerde gegevens. "
            "Een jaarrekening heeft twee of meer jaarkolommen (verslagjaar en vergelijkende jaren). 'jaren' = de "
            "jaartallen van ALLE kolommen in de volgorde van de kolommen, nieuwste eerst (bijv. [2024, 2023]). "
            "Geef per post de bedragen van alle kolommen in diezelfde volgorde ('bedragen' heeft evenveel getallen als "
            "'jaren'; gebruik 0 waar een kolom leeg of nihil is). "
            "Neem elk bedrag exact over zoals het in de tekst staat, als geheel getal (12.345 -> 12345, -1.910 -> -1910). "
            "Laat posten weg die niet in de jaarrekening staan. Kosten zijn positief. 'financieel' is het saldo "
            "financiële baten en lasten met teken. "
            "Leasecontracten: per contract de naam zoals in de toelichting op de langlopende schulden, het langlopende "
            "bedrag per jaarkolom en de letterlijke beschrijvende alinea. afschrijvingen_tekst: de regels over de "
            "afschrijvingspercentages, letterlijk."
        )
        invoer = "POSTEN:\n" + "\n".join(f"{k}: {v}" for k, v in posten.items()) + "\n\nJAARREKENING:\n" + tekst
        return self._vraag("jaarrekening_uitlezen", system, invoer, schema, max_tokens=16000, effort="medium")

    # ------------------------------------------------------------------
    def koppel_regels(self, rijen: list[dict], bronnen: list[dict], bekend: list[dict],
                      feedback: list[dict] | None = None, instructies: str = "") -> list[dict]:
        """Formule per rapportregel in termen van Asperion-bedragen (alleen de regels die code niet zelf vond).

        rijen:   [{id, pagina, kop, groep, label, waarde_verslagjaar}]
        bronnen: [{sleutel, omschrijving, vorig}]  (rekening:/groep:/sectie:/resultaat)
        bekend:  [{id, label, waarde_verslagjaar}] regels die al vastliggen (bruikbaar als 'rij'-term)
        """
        ids = [r["id"] for r in rijen]
        schema = {
            "type": "object",
            "properties": {"formules": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "rij": {"type": "string", "enum": ids},
                    "termen": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"bron": {"type": "string",
                                                "enum": ["rekening", "groep", "sectie", "resultaat", "rij", "aflossing"]},
                                       "naam": {"type": "string"},
                                       "teken": {"type": "integer", "enum": [1, -1]}},
                        "required": ["bron", "naam", "teken"], "additionalProperties": False}},
                    "toelichting": {"type": "string"}},
                "required": ["rij", "termen", "toelichting"], "additionalProperties": False}}},
            "required": ["formules"], "additionalProperties": False,
        }
        system = (
            "Je koppelt regels uit een Nederlands jaarrapport aan bedragen uit de Asperion-balans/W&V, zodat het rapport "
            "voor het nieuwe jaar automatisch gevuld kan worden. Geef per regel een formule: een som van termen met "
            "teken +1 of -1. Bronnen:\n"
            "- rekening: naam = rekeningnummer (saldo debet min credit);\n"
            "- groep: naam = 'balans|<groepnaam>' of 'wv|<groepnaam>' (subtotaal van een groep, debet min credit);\n"
            "- sectie: naam = 'balans|<SECTIE>' of 'wv|<SECTIE>' (de 'Totaal ...'-regel, debet min credit);\n"
            "- resultaat: het resultaat volgens Asperion (winst positief, verlies negatief), naam leeg;\n"
            "- rij: naam = id van een andere rapportregel (voor totalen die Asperion niet toont);\n"
            "- aflossing: naam = rekeningnummer van een lening/lease; de afname van de schuld in het jaar "
            "(voor het kortlopende deel van een lening).\n"
            "Een regel die nihil is: lege termen. Debet min credit betekent: schulden, kapitaal en opbrengsten zijn "
            "negatief; gebruik teken -1 om ze positief te tonen zoals in het rapport. Bij 'na resultaatbestemming' "
            "zit het resultaat van het jaar in de reserves of het kapitaal. "
            "Regels die Asperion niet als één bedrag toont (zoals brutomarge, bedrijfsresultaat, activa- en "
            "passivatotalen) zijn een optelling van de regels erboven in hetzelfde rapport: gebruik dan rij-termen "
            "naar die regels, niet een andere route via Asperion-totalen. "
            "De formule moet met de bedragen 'vorig' van Asperion de waarde_verslagjaar van het rapport geven (marge "
            "2 euro). Toelichting: maximaal 8 woorden."
        )
        invoer = {"regels": rijen, "asperion": bronnen, "al_bekende_regels": bekend}
        if feedback:
            invoer["eerdere_poging_klopte_niet"] = feedback
        if instructies.strip():
            invoer["extra_instructies"] = instructies.strip()
            system += " " + EXTRA_REGEL + " Elke formule moet nog steeds het bewijs op 'vorig' halen."
        data = self._vraag("regels_koppelen", system, json.dumps(invoer, ensure_ascii=False, separators=(",", ":")),
                           schema, max_tokens=16000)
        return [f for f in data.get("formules", []) if f["rij"] in ids]

    # ------------------------------------------------------------------
    def pas_aan(self, vraag: str, regels: list[dict], asperion: list[dict], teksten: list[list],
                gesprek: list[dict] | None = None, instructies: str = "") -> dict:
        """Chat-aanpassing op een gemaakt jaarverslag. Geeft {antwoord, formules, teksten} terug;
        de code bewijst en controleert alles daarna zelf."""
        term = {"type": "object",
                "properties": {"bron": {"type": "string",
                                        "enum": ["rekening", "groep", "sectie", "resultaat", "rij", "aflossing"]},
                               "naam": {"type": "string"}, "teken": {"type": "integer", "enum": [1, -1]}},
                "required": ["bron", "naam", "teken"], "additionalProperties": False}
        schema = {
            "type": "object",
            "properties": {
                "antwoord": {"type": "string"},
                "formules": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"rij": {"type": "string"}, "termen": {"type": "array", "items": term},
                                   "toelichting": {"type": "string"}},
                    "required": ["rij", "termen", "toelichting"], "additionalProperties": False}},
                "teksten": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"regel": {"type": "string"}, "tekst": {"type": "string"}},
                    "required": ["regel", "tekst"], "additionalProperties": False}},
            },
            "required": ["antwoord", "formules", "teksten"], "additionalProperties": False,
        }
        system = (
            "Je helpt een samensteller bij een Nederlands administratiekantoor een concept-jaarverslag aan te passen. "
            "Je krijgt de regels met bedragen (id, pagina, tabel, omschrijving, 'oud' = vorig jaar in het oude rapport, "
            "'nieuw' = nieuw jaar, 'formule' = lijst [bron, naam, teken]), de Asperion-bedragen en de tekstregels "
            "([id, pagina, tekst]).\n"
            "Bedragen wijzig je ALLEEN via een nieuwe formule voor een regel (bronnen: rekening = rekeningnummer, "
            "groep = 'balans|<groep>' of 'wv|<groep>', sectie = 'balans|<SECTIE>' of 'wv|<SECTIE>', resultaat, "
            "rij = id van een andere regel, aflossing = rekeningnummer; debet min credit, gebruik teken -1 om "
            "schulden, kapitaal en opbrengsten positief te tonen). Tik nooit zelf een bedrag in. De code controleert "
            "elke formule: toegepast op 'vorig' van Asperion moet hij 'oud' geven (marge 2 euro), anders wordt de "
            "wijziging geweigerd. Pas ook regels aan die hierdoor moeten meeveranderen (totalen met andere routes).\n"
            "Tekst wijzig je per hele regel: geef het id en de volledige nieuwe tekst van die regel. Houd de lengte "
            "ongeveer gelijk (een regel loopt niet door naar de volgende); lege tekst = regel weghalen. Zet geen "
            "bedragen in tekst die niet al in de regels staan.\n"
            "Is het een vraag zonder wijziging: lege lijsten. Kan iets niet (bijv. een bedrag dat niet uit Asperion "
            "komt, of een nieuwe regel of tabel toevoegen), zeg dat kort.\n"
            "antwoord: Nederlands, hooguit 3 korte zinnen, zeg wat je wijzigt en waarom. Geen opsommingen."
        )
        invoer = {"vraag": vraag, "regels": regels, "asperion": asperion, "tekstregels": teksten}
        if gesprek:
            invoer["eerder_in_dit_gesprek"] = gesprek[-6:]
        if instructies.strip():
            invoer["extra_instructies"] = instructies.strip()
            system += "\n" + EXTRA_REGEL
        data = self._vraag("aanpassen", system, json.dumps(invoer, ensure_ascii=False, separators=(",", ":")),
                           schema, max_tokens=8000)
        return {"antwoord": data.get("antwoord", ""), "formules": data.get("formules", []),
                "teksten": data.get("teksten", [])}

    # ------------------------------------------------------------------
    @property
    def totaal(self) -> dict:
        return {"verzoeken": len(self.gebruik),
                "input_tokens": sum(g.input_tokens for g in self.gebruik),
                "output_tokens": sum(g.output_tokens for g in self.gebruik),
                "kosten_usd": round(sum(g.kosten_usd for g in self.gebruik), 5)}


# --------------------------------------------------------------------------
_BEDRAG = re.compile(r"(?<![\d.])-?\d{1,3}(?:\.\d{3})+(?![\d.])")


def bedragen_in_tekst(tekst: str) -> set[str]:
    """Bedragen in duizendtal-notatie (>= 1.000) uit een tekst, zonder minteken."""
    return {m.group(0).lstrip("-") for m in _BEDRAG.finditer(tekst)}


def toegestane_bedragen(obj) -> set[str]:
    """Alle bedragen uit de invoer (en verschillen tussen boekjaar en vorig jaar), in fmt-notatie."""
    from .structuur import fmt
    waarden: set[int] = set()

    def loop(o):
        if isinstance(o, dict):
            if set(o) >= {"boekjaar", "vorig_jaar"} and all(isinstance(o[k], int) for k in ("boekjaar", "vorig_jaar")):
                waarden.add(o["boekjaar"] - o["vorig_jaar"])
            for v in o.values():
                loop(v)
        elif isinstance(o, list):
            for v in o:
                loop(v)
        elif isinstance(o, bool):
            return
        elif isinstance(o, int):
            waarden.add(o)
        elif isinstance(o, str):
            for b in bedragen_in_tekst(o):
                waarden.add(int(b.replace(".", "")))

    loop(obj)
    return {fmt(abs(w)) for w in waarden}
