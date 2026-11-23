"""Opslag per klant en kantoorinstellingen (JSON-bestanden in mappen)."""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from . import KANTOOR_BESTAND, KLANTEN, STANDAARD_MAPPING

KANTOOR_STANDAARD = {"naam": "", "plaats": "", "ondertekenaar": "",
                     "signaal_drempel_bedrag": 5000, "signaal_drempel_procent": 25, "ai_effort": "medium"}


def _lees(pad: Path, standaard=None):
    if not pad.exists():
        return standaard
    return json.loads(pad.read_text(encoding="utf-8"))


def _schrijf(pad: Path, data) -> None:
    pad.parent.mkdir(parents=True, exist_ok=True)
    tmp = pad.with_suffix(pad.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(pad)


def laad_kantoor(pad: Path = KANTOOR_BESTAND) -> dict:
    return {**KANTOOR_STANDAARD, **(_lees(pad, {}) or {})}


def bewaar_kantoor(gegevens: dict, pad: Path = KANTOOR_BESTAND) -> None:
    _schrijf(pad, gegevens)


def slug(naam: str) -> str:
    s = unicodedata.normalize("NFKD", naam).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "klant"


class Klant:
    def __init__(self, map: Path):
        self.map = Path(map)
        self.id = self.map.name

    # --- gegevens ----------------------------------------------------------
    @property
    def gegevens(self) -> dict:
        return _lees(self.map / "klant.json", {})

    @property
    def naam(self) -> str:
        return self.gegevens.get("naam", self.id)

    def bewaar_gegevens(self, gegevens: dict) -> None:
        _schrijf(self.map / "klant.json", gegevens)

    # --- koppelingen -------------------------------------------------------
    @property
    def mapping(self) -> dict:
        m = _lees(self.map / "mapping.json", None)
        if m is None:
            raise FileNotFoundError(f"Geen mapping.json voor klant {self.id}")
        m.setdefault("ai_voorstellen", {})
        m.setdefault("leasecontracten", {})
        return m

    def bewaar_mapping(self, mapping: dict) -> None:
        _schrijf(self.map / "mapping.json", mapping)

    # --- jaarrekeningen ----------------------------------------------------
    def jaren(self) -> list[int]:
        return sorted(int(m.group(1)) for p in self.map.glob("jaarrekening_*.json")
                      if (m := re.match(r"jaarrekening_(\d{4})\.json$", p.name)))

    def jaarrekening(self, jaar: int) -> dict | None:
        return _lees(self.map / f"jaarrekening_{jaar}.json", None)

    def bewaar_jaarrekening(self, jaar: int, posten: dict, status: str, bron: str,
                            vergelijkend: list[dict] | None = None) -> Path:
        """posten = het verslagjaar; vergelijkend = de overige jaarkolommen [{jaar, posten}], nieuwste eerst.
        Het aantal jaarkolommen (1 + vergelijkend) wordt in het volgende verslag aangehouden."""
        vergelijkend = vergelijkend or []
        pad = self.map / f"jaarrekening_{jaar}.json"
        _schrijf(pad, {"_toelichting": f"Cijfers jaarrekening {jaar} ({status}); vergelijkende cijfers voor {jaar + 1}.",
                       "jaar": jaar, "bedrijf": self.naam, "status": status, "bron": bron,
                       "aantal_jaren": 1 + len(vergelijkend),
                       "opgeslagen": datetime.now().isoformat(timespec="seconds"), "posten": posten,
                       "vergelijkend": vergelijkend})
        return pad

    def output_map(self, jaar: int) -> Path:
        p = self.map / "output" / str(jaar)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def log_ai_gebruik(self, regels: list[dict]) -> None:
        if not regels:
            return
        with open(self.map / "ai_gebruik.jsonl", "a", encoding="utf-8") as f:
            for r in regels:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def alle_klanten(root: Path = KLANTEN) -> list[Klant]:
    root.mkdir(parents=True, exist_ok=True)
    return sorted((Klant(p) for p in root.iterdir() if (p / "klant.json").exists()), key=lambda k: k.naam.lower())


def nieuwe_klant(naam: str, gegevens: dict, mapping: dict, jaar: int, posten: dict,
                 root: Path = KLANTEN, overschrijf: bool = False, vergelijkend: list[dict] | None = None) -> Klant:
    map = root / slug(naam)
    if (map / "klant.json").exists() and not overschrijf:
        raise FileExistsError(f"Klant '{naam}' bestaat al ({map.name}).")
    k = Klant(map)
    k.bewaar_gegevens({"naam": naam, **gegevens})
    k.bewaar_mapping(mapping)
    k.bewaar_jaarrekening(jaar, posten, status="vastgesteld", bron="ingelezen uit jaarverslag-PDF",
                          vergelijkend=vergelijkend)
    return k


def standaard_mapping(pad: Path = STANDAARD_MAPPING) -> dict:
    return _lees(pad)
