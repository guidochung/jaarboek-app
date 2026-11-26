"""Jaarboek: oud jaarverslag + nieuwe balans -> nieuw jaarverslag (één venster).

Starten:  cd ~/Desktop/Jaarboek\\ app && streamlit run app.py
(De uitgebreide versie met klantbeheer en koppelingen: streamlit run app_uitgebreid.py)
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import html
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jaarboek import KANTOOR_BESTAND, KLANTEN  # noqa: E402
from jaarboek.ai import MODEL, AIFout, ClaudeAI  # noqa: E402
from jaarboek.dossier import laad_kantoor  # noqa: E402
from jaarboek.doorrol import STAPPEN, DoorrolFout, bronnen, formule_tekst, pas_aan, rol_door  # noqa: E402
from jaarboek.inlezen import InvoerFout  # noqa: E402
from jaarboek.omzet import (TEGENREKENING_LABELS, OmzetFout, bewaar_koppeling, euro_nl,  # noqa: E402
                            koppeling_uit_json, laad_koppeling, lees_kassarapporten, maak_boeking, naar_fmutac,
                            prefix_uit_naam, stel_koppeling_voor)
from jaarboek.render import RenderFout, pdf_pagina  # noqa: E402
from jaarboek.structuur import MAANDEN, fmt  # noqa: E402

KANTOOR_PAD = Path(os.environ.get("JAARBOEK_KANTOOR", KANTOOR_BESTAND))
CONTAINER = bool(os.environ.get("JAARBOEK_CONTAINER")) or sys.platform.startswith("linux")   # gehost
OPRUIMEN_NA_UUR = 6                                          # uploads en uitvoer op de server
NEP_AI = os.environ.get("JAARBOEK_NEP_AI") == "1"
STAP_LABELS = STAPPEN
ERNST = {"hoog": "Controleren", "middel": "Navragen", "laag": "Ter info"}
STATUS_KLEUR = {"OK": "rgba(33, 160, 80, 0.14)", "LET OP": "rgba(230, 150, 20, 0.18)", "FOUT": "rgba(220, 60, 60, 0.20)"}

st.set_page_config(page_title="Jaarboek", page_icon=":material/menu_book:", layout="wide",
                   initial_sidebar_state="expanded")
st.html("""<style>
  .block-container {padding-top: 3.6rem; padding-bottom: 4rem; max-width: 1180px;}
  section[data-testid="stSidebar"] {transition: width .25s ease, min-width .25s ease, transform .25s ease;}
  section[data-testid="stSidebar"] .block-container {padding-top: 1.6rem;}
  details {transition: background-color .2s ease;}

  .jb-merk {display: flex; gap: .7rem; align-items: center; margin: 0 0 1.1rem 0;}
  .jb-logo {width: 36px; height: 36px; border-radius: 10px; flex: none; display: flex; align-items: center;
            justify-content: center; color: #fff; font-weight: 700; font-size: 1.05rem;
            background: linear-gradient(135deg, #2457d6, #6a95ff); box-shadow: 0 2px 8px rgba(36, 87, 214, .25);}
  .jb-naam {font-weight: 700; font-size: 1.08rem; line-height: 1.15;}
  .jb-ondertitel {font-size: .78rem; opacity: .6;}
  .jb-status {display: inline-flex; align-items: center; gap: .45rem; font-size: .8rem; font-weight: 500;
              padding: .22rem .7rem; border-radius: 999px; margin-bottom: .6rem;}
  .jb-status.aan {background: rgba(33, 160, 80, .14); color: #1e9b52;}
  .jb-status.uit {background: rgba(128, 128, 128, .15);}
  .jb-status.demo {background: rgba(230, 150, 20, .16); color: #b7770f;}
  .jb-stip {width: 7px; height: 7px; border-radius: 50%; background: currentColor;}
  .jb-kantoor {margin: 1.6rem 0 0 .1rem; padding-top: 1.1rem; border-top: 1px solid rgba(128, 128, 128, .18);}
  .jb-kantoor img {width: 118px; height: auto; display: block; opacity: .97; transition: opacity .2s ease, transform .2s ease;}
  .jb-kantoor img:hover {opacity: 1; transform: translateY(-1px);}
  .jb-gebruik {font-size: .8rem; line-height: 1.55; opacity: .75; margin-top: .4rem;}
  .jb-gebruik b {font-weight: 600; opacity: 1;}

  .jb-kop {font-size: 2rem; font-weight: 700; letter-spacing: -.01em; margin: 0;}
  .jb-intro {opacity: .65; margin: .25rem 0 1.4rem 0;}
  .jb-stap {display: flex; align-items: center; gap: .55rem; font-weight: 600; margin-bottom: .35rem;}
  .jb-nr {width: 22px; height: 22px; border-radius: 50%; background: #2457d6; color: #fff; font-size: .74rem;
          display: inline-flex; align-items: center; justify-content: center; flex: none;}
  .jb-stap small {font-weight: 400; opacity: .55;}

  .jb-hero {display: flex; justify-content: space-between; align-items: flex-end; gap: 1rem; margin: .4rem 0 .9rem 0;}
  .jb-hero-titel {font-size: 1.45rem; font-weight: 700; line-height: 1.2;}
  .jb-hero-sub {opacity: .62; font-size: .88rem; margin-top: .15rem;}
  div[data-testid="stMetric"] {border: 1px solid rgba(128, 128, 128, .22); border-radius: .8rem; padding: .8rem 1rem;}

  .jb-punt {display: grid; grid-template-columns: 104px 1fr; gap: .9rem; align-items: start;
            padding: .7rem .2rem; border-bottom: 1px solid rgba(128, 128, 128, .16);}
  .jb-punt:last-child {border-bottom: none;}
  .jb-pil {font-size: .72rem; font-weight: 600; padding: .2rem .6rem; border-radius: 999px; justify-self: start;
           white-space: nowrap; margin-top: .1rem;}
  .jb-pil.hoog {background: rgba(220, 60, 60, .14); color: #d03c3c;}
  .jb-pil.middel {background: rgba(230, 150, 20, .17); color: #b7770f;}
  .jb-pil.laag {background: rgba(128, 128, 128, .15); opacity: .85;}
  .jb-titel {font-weight: 600; line-height: 1.35;}
  .jb-tekst {opacity: .7; font-size: .92rem; line-height: 1.45; margin-top: .1rem;}
  .jb-chat-tekst {line-height: 1.5;}
  .jb-chat-w {display: flex; gap: .5rem; font-size: .84rem; line-height: 1.4; margin-top: .3rem; opacity: .85;}
  .jb-chat-w span {font-weight: 700; flex: none; width: 1rem; text-align: center;}
  .jb-chat-w.ok span {color: #1e9b52;}
  .jb-chat-w.nee span {color: #d03c3c;}
  .jb-chat-w.let span {color: #b7770f;}
  .jb-let {font-size: .8rem; color: #b7770f; margin-top: .2rem;}
  .jb-bron {font-size: .78rem; opacity: .55; margin-top: .5rem;}
</style>""")


# --------------------------------------------------------------------------
# Hulpfuncties
# --------------------------------------------------------------------------
def ruim_oude_werkmappen() -> None:
    """Verwijder werkmappen van (andere) sessies die ouder zijn dan OPRUIMEN_NA_UUR."""
    grens = time.time() - OPRUIMEN_NA_UUR * 3600
    for m in Path(tempfile.gettempdir()).glob("jaarboek_*"):
        try:
            if m.is_dir() and m.stat().st_mtime < grens and str(m) != st.session_state.get("tmpdir"):
                shutil.rmtree(m, ignore_errors=True)
        except OSError:
            pass


def werkmap() -> Path:
    ruim_oude_werkmappen()
    if "tmpdir" not in st.session_state:
        st.session_state.tmpdir = tempfile.mkdtemp(prefix="jaarboek_")
    st.session_state.run = st.session_state.get("run", 0) + 1
    p = Path(st.session_state.tmpdir) / f"run_{st.session_state.run}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def bewaar_upload(upload, map: Path) -> Path:
    pad = map / Path(upload.name).name
    pad.write_bytes(upload.getvalue())
    return pad


def claude(kantoor: dict) -> ClaudeAI | None:
    if NEP_AI:
        if "_ai" not in st.session_state:
            sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
            from nep_claude import NepClaude
            st.session_state._ai = ClaudeAI(client=NepClaude(), effort=kantoor["ai_effort"])
        return st.session_state._ai
    sleutel = (st.session_state.get("api_key") or "").strip()
    if not sleutel:
        return None
    if st.session_state.get("_ai_sleutel") != sleutel:
        st.session_state._ai = ClaudeAI(api_key=sleutel, effort=kantoor["ai_effort"])
        st.session_state._ai_sleutel = sleutel
    return st.session_state._ai


def euro(n) -> str:
    return f"€ {fmt(n)}"


def dollar(x: float) -> str:
    return f"${x:.4f}".replace(".", ",")


@st.cache_data(show_spinner=False, max_entries=60)
def pagina_png(pad: str, gewijzigd: float, nr: int) -> bytes:
    buf = io.BytesIO()
    pdf_pagina(Path(pad), nr, schaal=1.4).save(buf, format="PNG")
    return buf.getvalue()


def controletabel(controles):
    df = pd.DataFrame(controles)[["status", "controle", "detail"]]
    df.columns = ["Status", "Controle", "Detail"]
    return df.style.apply(lambda r: [f"background-color: {STATUS_KLEUR[r['Status']]}"] * len(r), axis=1)


def punten_html(punten: list[dict]) -> str:
    rijen = []
    for p in punten:
        let = (f'<div class="jb-let">Bedrag {html.escape(", ".join(p["onbekende_bedragen"]))} komt niet uit de '
               "berekening; controleer deze tekst.</div>" if p.get("onbekende_bedragen") else "")
        rijen.append(
            f'<div class="jb-punt"><span class="jb-pil {p["ernst"]}">{ERNST[p["ernst"]]}</span>'
            f'<div><div class="jb-titel">{html.escape(p["titel"].rstrip("."))}</div>'
            f'<div class="jb-tekst">{html.escape(p["toelichting"])}</div>{let}</div></div>')
    return "".join(rijen)


def chat_antwoord_html(a: dict) -> str:
    regels = [f'<div class="jb-chat-tekst">{html.escape(a["antwoord"])}</div>']
    regels += [f'<div class="jb-chat-w ok"><span>✓</span>{html.escape(w)}</div>' for w in a["doorgevoerd"]]
    regels += [f'<div class="jb-chat-w nee"><span>✕</span>{html.escape(w)}</div>' for w in a["geweigerd"]]
    regels += [f'<div class="jb-chat-w let"><span>!</span>Controle {html.escape(c)}</div>'
               for c in a.get("controles", [])]
    return "".join(regels)


def toon_gebruik(ai) -> None:
    """Claude-gebruik van deze sessie in de zijbalk (als laatste, zodat deze run meetelt)."""
    if ai:
        t = ai.totaal
        demo = " (schatting)" if NEP_AI else ""
        gebruik_plek.html(f'<div class="jb-gebruik">Deze sessie{demo}<br><b>{dollar(t["kosten_usd"])}</b> · '
                          f'{fmt(t["input_tokens"] + t["output_tokens"])} tokens · {t["verzoeken"]} verzoek(en)</div>')


# --------------------------------------------------------------------------
# Omzet naar Asperion: kassarapporten -> kasboekboeking (FMUTAC-CSV), zonder Claude
# --------------------------------------------------------------------------
JAARVERSLAG, OMZET = "Jaarverslag", "Omzet naar Asperion"
ONDERTITEL = {JAARVERSLAG: "Jaarverslag uit de Asperion-balans", OMZET: "Kassa-omzet naar Asperion"}
OMZET_KLANTEN = Path(os.environ.get("JAARBOEK_KLANTEN", KLANTEN)) / "omzet"    # lokaal bewaarde koppelingen


def _slug(naam: str) -> str:
    s = unicodedata.normalize("NFKD", naam).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "klant"


@st.cache_data(show_spinner=False, max_entries=20)
def _lees_kassa(bestanden: tuple[tuple[str, bytes], ...]):
    return lees_kassarapporten(list(bestanden))


def _koppeling_voor(bedrijf: str) -> tuple[dict, str]:
    """Geüploade koppeling gaat voor; anders de lokaal bewaarde van deze klant; anders de standaard."""
    up = st.session_state.get("oz_upload")
    if up:
        return copy.deepcopy(up["koppeling"]), f"geüpload ({up['naam']})"
    pad = OMZET_KLANTEN / f"{_slug(bedrijf)}.json"
    if pad.exists():
        try:
            return koppeling_uit_json(pad.read_text(encoding="utf-8")), f"bewaard voor {bedrijf}"
        except OmzetFout as e:
            st.warning(f"De bewaarde koppeling voor {bedrijf} is onleesbaar ({e}); de standaard wordt gebruikt.",
                       icon=":material/warning:")
    return laad_koppeling(), "standaard"


def pagina_omzet() -> None:
    st.html('<p class="jb-kop">Omzet naar Asperion</p>'
            '<p class="jb-intro">Upload het dagrapport en het productrapport uit de kassa. Per kassacategorie komt er '
            'één omzetregel met btw op de juiste grootboekrekening. De boeking download je als FMUTAC-CSV voor het '
            'kasboek in Asperion. Hiervoor is geen Claude-sleutel nodig.</p>')
    k1, k2 = st.columns([3, 2], gap="medium")
    with k1:
        st.html('<div class="jb-stap"><span class="jb-nr">1</span>Kassarapporten '
                '<small>dagrapport en productrapport, Excel</small></div>')
        bestanden = st.file_uploader("Kassarapporten", type=["xlsx"], accept_multiple_files=True,
                                     key="oz_rapporten", label_visibility="collapsed")
    with k2:
        st.html('<div class="jb-stap"><span class="jb-nr">2</span>Koppeling <small>optioneel, eerder gedownload</small></div>')
        up = st.file_uploader("Koppeling", type=["json"], key="oz_koppeling_upload", label_visibility="collapsed")

    vorige = st.session_state.get("oz_upload")
    if up is None and vorige:
        st.session_state.pop("oz_upload")
        st.session_state.oz_versie = st.session_state.get("oz_versie", 0) + 1
    elif up is not None and (not vorige or vorige["id"] != up.file_id):
        try:
            st.session_state.oz_upload = {"id": up.file_id, "naam": up.name,
                                          "koppeling": koppeling_uit_json(up.getvalue())}
        except OmzetFout as e:
            st.session_state.pop("oz_upload", None)
            st.error(str(e), icon=":material/error:")
        st.session_state.oz_versie = st.session_state.get("oz_versie", 0) + 1

    if len(bestanden or []) < 2:
        st.caption("Nog nodig: " + ("het dagrapport en het productrapport." if not bestanden
                                    else "het tweede rapport (dag- of productrapport)."))
        return
    try:
        dag, prod = _lees_kassa(tuple((f.name, f.getvalue()) for f in bestanden))
    except OmzetFout as e:
        st.error(str(e), icon=":material/error:")
        return

    koppeling, bron = _koppeling_voor(dag.bedrijf)
    voorstel = stel_koppeling_voor(prod, koppeling)
    sleutel = f"{_slug(dag.bedrijf)}_{st.session_state.get('oz_versie', 0)}"
    sig = hashlib.sha1(b"".join(f.getvalue() for f in bestanden)).hexdigest()[:10]

    st.divider()
    kop, koppel_plek, controle_plek, regels_plek = st.container(), st.container(), st.container(), st.container()

    # Koppeltabel: omschrijving en grootboekrekening per kassacategorie en btw-tarief
    nieuw = [r for r in voorstel if not r.bekend or not r.rekening]
    with koppel_plek, st.expander(f"Koppeling omzetcategorieën · {len(voorstel)}", expanded=bool(nieuw),
                                  icon=":material/account_tree:"):
        st.caption(f"Koppeling: {bron}. Elke kassacategorie wordt één regel (alle producten opgeteld). "
                   "Een categorie met omzet in twee btw-tarieven krijgt per tarief een regel."
                   + (f" Nieuw in deze maand: {', '.join(dict.fromkeys(r.categorie for r in nieuw))}; "
                      "vul de grootboekrekening in." if nieuw else ""))
        df = pd.DataFrame([{"Kassacategorie": r.categorie, "Btw": f"{r.tarief}%", "Omschrijving": r.omschrijving,
                            "Grootboek": r.rekening, "Incl. btw": euro_nl(r.bruto), "Btw-bedrag": euro_nl(r.btw),
                            "Status": "" if r.bekend else "Nieuw"} for r in voorstel])
        bewerkt = st.data_editor(
            df, key=f"oz_editor_{sig}_{sleutel}", hide_index=True, width="stretch", num_rows="fixed",
            disabled=["Kassacategorie", "Btw", "Incl. btw", "Btw-bedrag", "Status"],
            column_config={
                "Omschrijving": st.column_config.TextColumn(width="medium", help="Komt na 'Omzet <periode>' in de boeking"),
                "Grootboek": st.column_config.TextColumn(validate=r"^\d+$", help="Rekeningnummer in Asperion"),
                "Incl. btw": st.column_config.TextColumn(alignment="right"),
                "Btw-bedrag": st.column_config.TextColumn(alignment="right")})
    for r, (_, rij) in zip(voorstel, bewerkt.iterrows()):
        r.omschrijving = str(rij["Omschrijving"] or "").strip() or r.categorie
        r.rekening = str(rij["Grootboek"] or "").strip()

    # Boekstuk en tegenrekeningen (onderaan de pagina)
    with st.expander("Boekstuk en tegenrekeningen", icon=":material/tune:"):
        c1, c2 = st.columns(2)
        prefix = c1.text_input("Boekstukprefix", value=str(koppeling.get("boekstuk_prefix") or ""),
                               placeholder=f"automatisch: {prefix_uit_naam(dag.bedrijf)}", key=f"oz_prefix_{sleutel}",
                               help="Boekstuknummer = prefix + maand + jaar, bijvoorbeeld BELL1026.")
        dagboek = c2.text_input("Dagboek", value=str(koppeling.get("dagboek") or "10"), key=f"oz_dagboek_{sleutel}")
        st.caption("Tegenrekeningen")
        kol, teg = st.columns(4), {}
        for i, (s, label) in enumerate(TEGENREKENING_LABELS.items()):
            teg[s] = kol[i % 4].text_input(label, value=str(koppeling["tegenrekeningen"].get(s, "")),
                                           key=f"oz_teg_{s}_{sleutel}").strip()
    koppeling = {**koppeling, "boekstuk_prefix": prefix.strip(), "dagboek": dagboek.strip() or "10",
                 "tegenrekeningen": teg}
    b = maak_boeking(dag, prod, voorstel, koppeling)
    nieuwe_koppeling = json.dumps(bewaar_koppeling(koppeling, voorstel), indent=2, ensure_ascii=False)

    with kop:
        st.html(f'<div class="jb-hero"><div><div class="jb-hero-titel">Omzet {MAANDEN[b.eind.month - 1]} '
                f'{b.eind.year} · {html.escape(b.bedrijf)}</div><div class="jb-hero-sub">Boekstuk {html.escape(b.boekstuk)}'
                f' · dagboek {html.escape(b.dagboek)} · {len(b.regels) - 1} boekingsregels · boekingsdatum '
                f'{b.eind:%d-%m-%Y}</div></div></div>')
        d1, d2, d3 = st.columns([1.2, 1.2, 1])
        d1.download_button("Download boeking (CSV)", b"" if b.fout else naar_fmutac(b), b.bestandsnaam, "text/csv",
                           type="primary", width="stretch", icon=":material/download:", disabled=b.fout)
        d2.download_button("Download koppeling", nieuwe_koppeling.encode("utf-8"),
                           f"omzet_koppeling_{_slug(b.bedrijf)}.json", "application/json", width="stretch",
                           icon=":material/file_download:",
                           help="Koppeltabel en instellingen; upload hem volgende maand bij stap 2.")
        if CONTAINER:
            d3.caption("Op de server wordt niets bewaard: download de koppeling en upload hem volgende maand weer.")
        elif d3.button("Bewaar koppeling", width="stretch", icon=":material/save:",
                       help=f"Bewaart koppeltabel en instellingen voor {b.bedrijf}; volgende maand worden ze "
                            "automatisch gebruikt."):
            OMZET_KLANTEN.mkdir(parents=True, exist_ok=True)
            (OMZET_KLANTEN / f"{_slug(b.bedrijf)}.json").write_text(nieuwe_koppeling + "\n", encoding="utf-8")
            st.toast(f"Koppeling bewaard voor {b.bedrijf}", icon=":material/check_circle:")
        if b.fout:
            st.error("Nog niet te downloaden. " + " · ".join(f"{c['controle']}: {c['detail']}"
                                                             for c in b.controles if c["status"] == "FOUT"),
                     icon=":material/error:")
        m = st.columns(4)
        m[0].metric("Omzet incl. btw", euro_nl(b.omzet_bruto))
        m[1].metric("Btw", euro_nl(b.omzet_btw))
        m[2].metric("Betalingen", euro_nl(b.betalingen))
        m[3].metric("Verschil", euro_nl(b.verschil), help="Sluitpost: betalingen min omzet incl. btw, fooi en "
                                                            "overige inkomsten.")
        st.write("")

    let_op = sum(c["status"] == "LET OP" for c in b.controles)
    with controle_plek, st.expander(f"Controles · {let_op} let op" if let_op and not b.fout else "Controles",
                                    expanded=b.fout, icon=":material/error:" if b.fout else ":material/task_alt:"):
        st.dataframe(controletabel(b.controles), hide_index=True, width="stretch",
                     column_config={"Detail": st.column_config.TextColumn(width="large")})

    with regels_plek, st.expander(f"Boekingsregels · {len(b.regels) - 1}", expanded=True,
                                  icon=":material/receipt_long:"):
        st.dataframe(pd.DataFrame([{"Nr": i, "Omschrijving": r.omschrijving, "Rekening": r.rekening,
                                    "Bedrag": euro_nl(r.bedrag), "Btw-code": r.btw_code, "Btw": euro_nl(r.btw_bedrag)}
                                   for i, r in enumerate(b.regels)]),
                     hide_index=True, width="stretch",
                     column_config={"Omschrijving": st.column_config.TextColumn(width="large"),
                                    "Bedrag": st.column_config.TextColumn(alignment="right"),
                                    "Btw": st.column_config.TextColumn(alignment="right")})


# --------------------------------------------------------------------------
# Zijbalk
# --------------------------------------------------------------------------
@st.cache_data
def _logo_b64(naam: str) -> str:
    return (Path(__file__).parent / "assets" / naam).read_text().strip()


def kantoor_logo() -> str:
    donker = getattr(st.context.theme, "type", None) == "dark"
    return "data:image/png;base64," + _logo_b64("pfc_logo_donker.b64" if donker else "pfc_logo_licht.b64")


# --------------------------------------------------------------------------
# Toegang (alleen als JAARBOEK_WACHTWOORD is gezet, bijv. als secret op Hugging Face)
# --------------------------------------------------------------------------
WACHTWOORD = os.environ.get("JAARBOEK_WACHTWOORD", "")
if WACHTWOORD and not st.session_state.get("toegang"):
    _, midden, _ = st.columns([1, 1.1, 1])
    with midden:
        st.write("")
        st.html('<div class="jb-merk"><div class="jb-logo">J</div><div><div class="jb-naam">Jaarboek</div>'
                '<div class="jb-ondertitel">Jaarverslag uit de Asperion-balans</div></div></div>')
        with st.form("inloggen", border=True):
            poging = st.text_input("Wachtwoord", type="password")
            if st.form_submit_button("Inloggen", type="primary", width="stretch"):
                if hmac.compare_digest(poging.encode(), WACHTWOORD.encode()):
                    st.session_state.toegang = True
                    st.rerun()
                st.error("Onjuist wachtwoord.", icon=":material/lock:")
        st.html(f'<div class="jb-kantoor" style="border:none;margin-top:.4rem"><img alt="PFC" src="{kantoor_logo()}"></div>')
    st.stop()

kantoor = laad_kantoor(KANTOOR_PAD)
with st.sidebar:
    st.html('<div class="jb-merk"><div class="jb-logo">J</div><div><div class="jb-naam">Jaarboek</div>'
            f'<div class="jb-ondertitel">{ONDERTITEL[st.session_state.get("onderdeel", JAARVERSLAG)]}</div></div></div>')
    if NEP_AI:
        st.html('<span class="jb-status demo"><span class="jb-stip"></span>Demo · Claude nagebootst</span>')
        ai = claude(kantoor)
    else:
        verbonden = bool((st.session_state.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")).strip())
        st.html(f'<span class="jb-status {"aan" if verbonden else "uit"}"><span class="jb-stip"></span>'
                f'{"Claude verbonden" if verbonden else "Claude niet verbonden"}</span>')
        with st.expander("API-sleutel", expanded=not verbonden, icon=":material/key:"):
            st.text_input("Claude API-sleutel", type="password", key="api_key", placeholder="sk-ant-…",
                          value="" if CONTAINER else os.environ.get("ANTHROPIC_API_KEY", ""),
                          label_visibility="collapsed")
            st.caption("Alleen gebruikt in deze sessie; niet opgeslagen.")
        ai = claude(kantoor)
    onderdeel = st.radio("Onderdeel", [JAARVERSLAG, OMZET], key="onderdeel",
                         captions=["Uit oud jaarverslag en nieuwe balans", "Kassa-omzet als FMUTAC-CSV"])
    gebruik_plek = st.empty()
    st.caption(f"Claude Opus 5.5 · effort {kantoor['ai_effort']}")
    st.html(f'<div class="jb-kantoor"><img alt="PFC" src="{kantoor_logo()}"></div>')

if onderdeel == OMZET:
    pagina_omzet()
    toon_gebruik(ai)
    st.stop()


# --------------------------------------------------------------------------
# Invoer
# --------------------------------------------------------------------------
st.html('<p class="jb-kop">Nieuw jaarverslag</p>'
        '<p class="jb-intro">Upload het jaarverslag van vorig jaar en de nieuwe balans. Het aantal jaarkolommen blijft '
        'gelijk: het nieuwe jaar komt vooraan, het oudste jaar valt weg.</p>')

k1, k2, k3 = st.columns([3, 3, 1.4], gap="medium")
with k1:
    st.html('<div class="jb-stap"><span class="jb-nr">1</span>Jaarverslag vorig jaar <small>PDF</small></div>')
    oud = st.file_uploader("Jaarverslag vorig jaar", type=["pdf"], key="oud", label_visibility="collapsed")
with k2:
    st.html('<div class="jb-stap"><span class="jb-nr">2</span>Nieuwe balans <small>PDF of Excel uit Asperion</small></div>')
    balans = st.file_uploader("Nieuwe balans", type=["pdf", "xlsx"], key="balans", label_visibility="collapsed")
with k3:
    st.html('<div class="jb-stap">Datum</div>')
    datum = st.date_input("Datum samenstelling", date.today(), format="DD-MM-YYYY", label_visibility="collapsed")

instructies = st.text_area(
    "Extra instructies (optioneel)", key="instructies", height=68,
    placeholder="Bijv.: de lening van de DGA is in 2025 volledig afgelost; rekening 1240 hoort bij overige vorderingen.",
    help="Wordt meegegeven aan Claude bij het koppelen en de aandachtspunten. Bedragen blijven altijd uit de balans komen.")

klaar = oud is not None and balans is not None and ai is not None
if st.button("Maak jaarverslag", type="primary", disabled=not klaar, icon=":material/auto_awesome:"):
    st.session_state.pop("resultaat", None)
    map = werkmap()
    oud_pad, balans_pad = bewaar_upload(oud, map), bewaar_upload(balans, map)
    with st.status("Bezig…", expanded=True) as status:
        def voortgang(stap, detail=""):
            if detail:
                status.write(f"**{STAP_LABELS.get(stap, stap)}** · {detail}")
            else:
                status.update(label=f"{STAP_LABELS.get(stap, stap)}…")
        try:
            st.session_state.resultaat = rol_door(oud_pad, balans_pad, datum, kantoor, ai, map / "uitvoer",
                                                  voortgang=voortgang, instructies=instructies)
            status.update(label="Jaarverslag klaar", state="complete", expanded=False)
        except (DoorrolFout, InvoerFout, RenderFout, AIFout) as e:
            status.update(label="Dat lukte niet", state="error", expanded=False)
            st.error(str(e), icon=":material/error:")
if not klaar:
    ontbreekt = [t for t, ok in (("het oude jaarverslag", oud), ("de nieuwe balans", balans), ("een API-sleutel", ai))
                 if not ok]
    st.caption("Nog nodig: " + ", ".join(ontbreekt) + ".")


# --------------------------------------------------------------------------
# Resultaat
# --------------------------------------------------------------------------
res = st.session_state.get("resultaat")
if res:
    st.divider()
    regels = list(res.uitkomsten.values())
    bewezen = sum(1 for u in regels if u.status == "OK")
    st.html(f'<div class="jb-hero"><div><div class="jb-hero-titel">Jaarverslag {res.jaar} · '
            f'{html.escape(res.bedrijf)}</div><div class="jb-hero-sub">{len(res.rapport.paginas)} pagina\'s · '
            f'{bewezen} van {len(regels)} regels bewezen op {res.jaar - 1} · concept van {res.datum}</div></div></div>')

    d1, d2, d3 = st.columns([1.2, 1.2, 1])
    d1.download_button("Download concept", res.pdf.read_bytes(), res.pdf.name, "application/pdf",
                       type="primary", width="stretch", icon=":material/download:")
    d2.download_button("Download zonder watermerk", res.definitief.read_bytes(), res.definitief.name,
                       "application/pdf", width="stretch", icon=":material/download:")
    d3.download_button("Controlerapport", res.rapport_md.read_bytes(), res.rapport_md.name, "text/markdown",
                       width="stretch", icon=":material/description:")
    for w in res.waarschuwingen:
        st.warning(w, icon=":material/warning:")

    if res.kerncijfers:
        m = st.columns(4)
        for kol, (lab, nieuw, oud_w) in zip(m, res.kerncijfers[:4]):
            kol.metric(lab, euro(nieuw), f"{fmt(nieuw - oud_w)} t.o.v. {res.jaar - 1}")

    st.write("")
    with st.expander("Aanpassen met Claude", expanded=bool(res.aanpassingen), icon=":material/forum:"):
        if not res.aanpassingen:
            st.html('<div class="jb-tekst">Vraag een wijziging in een tekst of in de koppeling van een regel, '
                    'bijvoorbeeld <i>"rekening 1240 hoort bij overige vorderingen"</i>. Bedragen komen altijd uit '
                    'de balans: wat niet klopt op vorig jaar wordt niet doorgevoerd.</div>')
        else:
            with st.container(height=420 if len(res.aanpassingen) > 2 else "content", border=False):
                for a in res.aanpassingen:
                    with st.chat_message("user", avatar=":material/person:"):
                        st.markdown(a["vraag"])
                    with st.chat_message("assistant", avatar=":material/auto_awesome:"):
                        st.html(chat_antwoord_html(a))
        vraag = st.chat_input("Wat wil je aanpassen?", key="chat_vraag", disabled=ai is None)
        if vraag:
            with st.spinner("Aanpassen en opnieuw controleren…"):
                try:
                    gesprek = [{"vraag": a["vraag"], "antwoord": a["antwoord"]} for a in res.aanpassingen]
                    item = pas_aan(res, vraag, ai, gesprek)
                except (DoorrolFout, RenderFout, AIFout) as e:
                    st.error(str(e), icon=":material/error:")
                else:
                    if item["paginas"]:
                        st.session_state["voorbeeld_pagina"] = item["paginas"][0]
                        st.toast("Jaarverslag bijgewerkt", icon=":material/check_circle:")
                    st.rerun()

    punten = res.aandachtspunten
    bron = ("Opgesteld door Claude Opus 5.5" + (" (nagebootst)" if NEP_AI else "")
            if res.aandachtspunten_bron == "ai" else "Opgesteld zonder AI")
    with st.expander(f"Aandachtspunten · {len(punten)}", expanded=True, icon=":material/flag:"):
        st.html((punten_html(punten) if punten else '<div class="jb-tekst">Geen aandachtspunten.</div>')
                + f'<div class="jb-bron">{bron}</div>')

    fout = res.status_telling["FOUT"]
    with st.expander("Controles", expanded=bool(fout),
                     icon=":material/error:" if fout else ":material/task_alt:"):
        st.dataframe(controletabel(res.controles), hide_index=True, width="stretch",
                     column_config={"Detail": st.column_config.TextColumn(width="large")})

    with st.expander(f"Herkomst per regel · {len(regels)}", icon=":material/account_tree:"):
        st.caption(f"Elke regel: bedrag {res.jaar - 1} in het oude rapport, dezelfde formule toegepast op de "
                   f"Asperion-kolom {res.jaar - 1} (bewijs) en op {res.jaar} (nieuw).")
        B = res.bronnen
        df = pd.DataFrame([{"Status": u.status, "Pag.": u.tabel.pagina, "Tabel": u.tabel.kop,
                            "Regel": u.rij.label or f"totaal {u.rij.groep}".strip(),
                            f"Rapport {res.jaar - 1}": fmt(u.oud),
                            "Bewijs": fmt(u.bewijs) if u.bewijs is not None else "—",
                            f"Nieuw {res.jaar}": fmt(u.nieuw) if u.nieuw is not None else "?",
                            "Door": u.bron, "Formule": formule_tekst(u.termen, B)} for u in regels])
        st.dataframe(df.style.apply(lambda r: [f"background-color: {STATUS_KLEUR[r['Status']]}"] * len(r), axis=1),
                     hide_index=True, width="stretch", column_config={"Formule": st.column_config.TextColumn(width="large")})

    with st.expander("Voorbeeld", icon=":material/visibility:"):
        st.session_state.setdefault("voorbeeld_pagina", min(6, len(res.rapport.paginas)))
        nr = st.number_input("Pagina", 1, len(res.rapport.paginas), key="voorbeeld_pagina")
        st.image(pagina_png(str(res.pdf), res.pdf.stat().st_mtime, nr - 1), width="stretch")


# Gebruik in de zijbalk (als laatste, zodat deze run meetelt)
toon_gebruik(ai)
