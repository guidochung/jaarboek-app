"""Claude-onderdelen met een nagebootste client (geen kosten, geen sleutel nodig)."""
import json
from types import SimpleNamespace

import pytest
from conftest import DATUM, XLSX, pdf_tekst
from nep_claude import NepClaude

from jaarboek.ai import MODEL, AIFout, ClaudeAI, bedragen_in_tekst, kosten, toegestane_bedragen
from jaarboek.berekenen import KoppelFout
from jaarboek.pipeline import maak_concept


def test_kosten_opus_55():
    assert kosten(1_000_000, 0) == pytest.approx(4.00)
    assert kosten(0, 1_000_000) == pytest.approx(20.00)
    assert kosten(2000, 1000) == pytest.approx(0.028)


def test_aandachtspunten_zuinig_verzoek(klant, kantoor, tmp_path):
    nep = NepClaude()
    ai = ClaudeAI(client=nep)
    concept = maak_concept(klant, XLSX, DATUM, kantoor, ai=ai, uitmap=tmp_path)
    assert concept.aandachtspunten_bron == "ai" and concept.aandachtspunten
    assert len(nep.verzoeken) == 1                       # alle rekeningen bekend: precies één AI-verzoek
    kw = nep.verzoeken[0]
    assert kw["model"] == MODEL == "claude-opus-5-5"
    assert kw["output_config"]["effort"] == "medium"
    assert kw["extra_body"] == {"fallbacks": "default"}
    assert len(kw["system"]) + len(kw["messages"][0]["content"]) < 6000   # compacte invoer, geen balans/PDF
    assert "1220" not in kw["messages"][0]["content"]                       # geen grootboekrekeningen naar Claude
    assert concept.gebruik and concept.kosten_usd > 0
    assert "AI-gebruik" in concept.rapport.read_text(encoding="utf-8")
    assert (klant.map / "ai_gebruik.jsonl").exists()


def test_verzonnen_bedrag_wordt_gemarkeerd(klant, kantoor, tmp_path):
    ai = ClaudeAI(client=NepClaude(verzin_bedrag=True))
    punten = maak_concept(klant, XLSX, DATUM, kantoor, ai=ai, uitmap=tmp_path).aandachtspunten
    assert punten[0]["onbekende_bedragen"] == ["12.345"]
    assert all(not p["onbekende_bedragen"] for p in punten[1:])


def test_bedragcontrole_hulpfuncties():
    assert bedragen_in_tekst("van 72.844 naar 34.274 (-53%) in 2025, -1.910") == {"72.844", "34.274", "1.910"}
    t = toegestane_bedragen({"kern": {"x": {"boekjaar": 34274, "vorig_jaar": 72844}}, "s": ["vorig jaar 6.050"]})
    assert {"34.274", "72.844", "38.570", "6.050"} <= t


def test_weigering_valt_terug_op_code(klant, kantoor, tmp_path):
    concept = maak_concept(klant, XLSX, DATUM, kantoor, ai=ClaudeAI(client=NepClaude(weiger=True)), uitmap=tmp_path)
    assert concept.aandachtspunten_bron == "code" and concept.aandachtspunten
    assert any("Claude niet beschikbaar" in w for w in concept.waarschuwingen)
    assert concept.pdf.exists()


def test_fallback_niet_beschikbaar_dan_zonder(monkeypatch):
    class BadRequestError(Exception):
        pass

    pogingen = []

    def create(**kw):
        pogingen.append(kw)
        if "extra_body" in kw:
            raise BadRequestError("fallbacks: unsupported")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"aandachtspunten": []}))],
                               stop_reason="end_turn", model=MODEL,
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))

    ai = ClaudeAI(client=SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))))
    assert ai.aandachtspunten({"signalen": []}) == []
    assert len(pogingen) == 2 and "extra_body" not in pogingen[1] and ai.fallbacks is False


def test_nieuwe_rekening_door_claude_gekoppeld(klant, kantoor, golden_posten, golden_tekst, tmp_path):
    m = klant.mapping
    m["rekeningen"].pop("4086")                       # werkkleding: doe alsof deze rekening nieuw is
    klant.bewaar_mapping(m)

    with pytest.raises(KoppelFout) as fout:
        maak_concept(klant, XLSX, DATUM, kantoor, ai=None, uitmap=tmp_path)
    assert [r["nr"] for r in fout.value.rekeningen] == ["4086"]

    nep = NepClaude(koppel={"4086": "alg_werkkleding"})
    concept = maak_concept(klant, XLSX, DATUM, kantoor, ai=ClaudeAI(client=nep), uitmap=tmp_path)
    assert [k["nr"] for k in concept.ai_koppelingen] == ["4086"]
    voorstel = klant.mapping["ai_voorstellen"]["4086"]
    assert voorstel["post"] == "alg_werkkleding" and voorstel["goedgekeurd"] is False
    assert any(k["controle"].startswith("Koppelingen door Claude") and k["status"] == "LET OP"
               for k in concept.controles)
    # De opties die Claude kreeg zijn alleen W&V-posten, en de gekozen post moet daarin zitten
    opties = nep.verzoeken[0]["output_config"]["format"]["schema"]["properties"]["voorstellen"]["items"]["properties"]["post"]["enum"]
    assert "alg_werkkleding" in opties and "crediteuren" not in opties
    assert {k: concept.c[k] for k in golden_posten} == golden_posten
    assert pdf_tekst(concept.pdf) == golden_tekst


def test_ongeldig_antwoord_geeft_aifout():
    def create(**kw):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="geen json")], stop_reason="end_turn",
                               model=MODEL, usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    ai = ClaudeAI(client=SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))))
    with pytest.raises(AIFout):
        ai.aandachtspunten({"signalen": []})
