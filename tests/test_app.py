"""De Streamlit-apps starten zonder fouten."""
import shutil

from conftest import BASIS
from streamlit.testing.v1 import AppTest


def _omgeving(monkeypatch, tmp_path, klanten_root=None):
    kantoor = tmp_path / "kantoor.json"
    shutil.copy(BASIS / "kantoor.json", kantoor)
    monkeypatch.setenv("JAARBOEK_KANTOOR", str(kantoor))
    if klanten_root:
        monkeypatch.setenv("JAARBOEK_KLANTEN", str(klanten_root))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("JAARBOEK_NEP_AI", raising=False)


def test_eenvoudige_app(tmp_path, monkeypatch):
    _omgeving(monkeypatch, tmp_path)
    at = AppTest.from_file(str(BASIS / "app.py"), default_timeout=60).run()
    assert not at.exception
    knop = next(b for b in at.button if b.label == "Maak jaarverslag")
    assert knop.disabled                         # zonder bestanden en sleutel niet te klikken
    assert not at.tabs                            # één venster
    assert at.text_input(key="api_key").value == ""              # sleutelveld (zijbalk), leeg zonder sleutel


def test_uitgebreide_app(klanten_root, tmp_path, monkeypatch):
    _omgeving(monkeypatch, tmp_path, klanten_root)
    at = AppTest.from_file(str(BASIS / "app_uitgebreid.py"), default_timeout=60).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Maak concept", "Nieuwe klant", "Koppelingen en klantgegevens"]
    tabellen = [df for df in at.dataframe if "Rekening" in df.value.columns]
    assert tabellen and len(tabellen[0].value) == 63


def test_wachtwoord_houdt_de_app_dicht(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("JAARBOEK_WACHTWOORD", "geheim")
    at = AppTest.from_file(str(BASIS / "app.py")).run(timeout=60)
    assert [t.label for t in at.text_input] == ["Wachtwoord"] and not at.file_uploader
    at.text_input[0].input("fout")
    at.button[0].click().run(timeout=60)
    assert at.error and not at.file_uploader
    at.text_input[0].input("geheim")
    at.button[0].click().run(timeout=60)
    assert "Extra instructies (optioneel)" in [t.label for t in at.text_area]
