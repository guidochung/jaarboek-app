"""Jaarboek-app: concept-jaarrekening uit de Asperion-balans met één knop.

Starten:  streamlit run app.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jaarboek import KANTOOR_BESTAND, KLANTEN  # noqa: E402
from jaarboek.ai import MODEL, AIFout, ClaudeAI  # noqa: E402
from jaarboek.berekenen import KoppelFout  # noqa: E402
from jaarboek.dossier import Klant, alle_klanten, bewaar_kantoor, laad_kantoor  # noqa: E402
from jaarboek.inlezen import InvoerFout  # noqa: E402
from jaarboek.onboarding import bewaar_als_klant, hercontroleer, lees_vorige_jaarrekening  # noqa: E402
from jaarboek.pipeline import STAPPEN, DossierFout, maak_concept, stel_vast  # noqa: E402
from jaarboek.render import RenderFout, pdf_paginas  # noqa: E402
from jaarboek.structuur import (BALANS_REKENINGPOSTEN, WV_REKENINGPOSTEN, fmt,  # noqa: E402
                                labels_voor)

KLANTEN_ROOT = Path(os.environ.get("JAARBOEK_KLANTEN", KLANTEN))
KANTOOR_PAD = Path(os.environ.get("JAARBOEK_KANTOOR", KANTOOR_BESTAND))
NEP_AI = os.environ.get("JAARBOEK_NEP_AI") == "1"

st.set_page_config(page_title="Jaarboek", page_icon=":material/menu_book:", layout="wide")
st.markdown("""<style>
  .block-container {padding-top: 2rem;}
  div[data-testid="stMetricValue"] {font-size: 1.6rem;}
</style>""", unsafe_allow_html=True)

ERNST_KLEUR = {"hoog": "red", "middel": "orange", "laag": "gray"}
# Halfdoorzichtige kleuren: leesbaar in zowel licht als donker thema
STATUS_KLEUR = {"OK": "rgba(33, 160, 80, 0.16)", "LET OP": "rgba(240, 150, 20, 0.22)", "FOUT": "rgba(220, 50, 50, 0.24)"}
SCHATTING = " (demo: geschat, niet echt)" if NEP_AI else ""


# --------------------------------------------------------------------------
# Hulpfuncties
# --------------------------------------------------------------------------
def euro(n) -> str:
    return f"€ {fmt(n)}"


def tijdelijk_bestand(upload) -> Path:
    if "tmpdir" not in st.session_state:
        st.session_state.tmpdir = tempfile.mkdtemp(prefix="jaarboek_")
    pad = Path(st.session_state.tmpdir) / Path(upload.name).name
    pad.write_bytes(upload.getvalue())
    return pad


def claude(kantoor: dict) -> ClaudeAI | None:
    """Eén ClaudeAI-object per sessie (houdt het tokengebruik bij)."""
    if NEP_AI:
        if "_ai" not in st.session_state:
            sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
            from nep_claude import NepClaude
            st.session_state._ai = ClaudeAI(client=NepClaude(koppel={}), effort=kantoor["ai_effort"])
        return st.session_state._ai
    sleutel = (st.session_state.get("api_key") or "").strip()
    if not sleutel:
        return None
    if st.session_state.get("_ai_sleutel") != sleutel:
        st.session_state._ai = ClaudeAI(api_key=sleutel, effort=kantoor["ai_effort"])
        st.session_state._ai_sleutel = sleutel
    st.session_state._ai.effort = kantoor["ai_effort"]
    return st.session_state._ai


@st.cache_data(show_spinner=False, max_entries=60)
def pagina_png(pad: str, gewijzigd: float, nr: int) -> bytes:
    import io
    img = pdf_paginas(Path(pad), schaal=1.4)[nr]
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@st.cache_data(show_spinner=False)
def aantal_paginas(pad: str, gewijzigd: float) -> int:
    import pypdfium2 as pdfium
    from jaarboek import PDFIUM_SLOT
    with PDFIUM_SLOT:
        doc = pdfium.PdfDocument(pad)
        try:
            return len(doc)
        finally:
            doc.close()


def controletabel(controles: list[dict]):
    df = pd.DataFrame(controles)[["status", "controle", "detail"]]
    df.columns = ["Status", "Controle", "Detail"]
    return df.style.apply(lambda r: [f"background-color: {STATUS_KLEUR[r['Status']]}"] * len(r), axis=1)


def toon_pdf(pad: Path, sleutel: str):
    n = aantal_paginas(str(pad), pad.stat().st_mtime)
    nr = st.number_input("Pagina", 1, n, 6 if n >= 6 else 1, key=f"pagina_{sleutel}")
    st.image(pagina_png(str(pad), pad.stat().st_mtime, nr - 1), width="stretch")


# --------------------------------------------------------------------------
# Zijbalk
# --------------------------------------------------------------------------
kantoor = laad_kantoor(KANTOOR_PAD)
with st.sidebar:
    st.title("Jaarboek")
    st.caption("Concept-jaarrekening uit de Asperion-balans")
    if NEP_AI:
        st.warning("Demomodus: Claude wordt nagebootst (geen echte AI, geen kosten).")
    else:
        st.text_input("Claude API-sleutel", type="password", key="api_key",
                      value=os.environ.get("ANTHROPIC_API_KEY", ""),
                      help="Nodig voor de aandachtspunten van Claude, koppelvoorstellen en het inlezen van nieuwe klanten. "
                           "Zonder sleutel werkt de rest gewoon.")
    ai = claude(kantoor)
    if ai:
        st.success(f"Claude Opus 5.5 actief (effort {kantoor['ai_effort']})", icon=":material/check_circle:")
        kosten_plek = st.container()  # pas aan het eind gevuld, zodat het gebruik van deze run meetelt
    else:
        kosten_plek = None
    if not ai:
        st.info("Zonder sleutel: concept, controles en PDF werken; aandachtspunten komen dan uit de code.",
                icon=":material/info:")

    with st.expander("Kantoorgegevens"):
        with st.form("kantoor"):
            k_naam = st.text_input("Naam kantoor", kantoor["naam"])
            k_plaats = st.text_input("Plaats", kantoor["plaats"])
            k_onder = st.text_input("Ondertekenaar", kantoor["ondertekenaar"])
            k_bedrag = st.number_input("Signaal vanaf (euro)", 0, 1_000_000, int(kantoor["signaal_drempel_bedrag"]), 500)
            k_pct = st.number_input("en mutatie vanaf (%)", 0, 1000, int(kantoor["signaal_drempel_procent"]), 5)
            k_effort = st.selectbox("Claude effort", ["low", "medium", "high"],
                                    ["low", "medium", "high"].index(kantoor["ai_effort"]),
                                    help="Hoger = grondiger maar meer tokens. 'low' is het zuinigst.")
            if st.form_submit_button("Opslaan"):
                bewaar_kantoor({**kantoor, "naam": k_naam, "plaats": k_plaats, "ondertekenaar": k_onder,
                                "signaal_drempel_bedrag": k_bedrag, "signaal_drempel_procent": k_pct,
                                "ai_effort": k_effort}, KANTOOR_PAD)
                st.rerun()
    st.caption(f"Model: `{MODEL}`")

klanten = alle_klanten(KLANTEN_ROOT)
tab_concept, tab_nieuw, tab_koppel = st.tabs(["Maak concept", "Nieuwe klant", "Koppelingen en klantgegevens"])


# --------------------------------------------------------------------------
# Tab 1: Maak concept
# --------------------------------------------------------------------------
with tab_concept:
    if not klanten:
        st.info("Er zijn nog geen klanten. Voeg eerst een klant toe via het tabblad **Nieuwe klant**.")
    else:
        c1, c2, c3 = st.columns([2, 3, 1.3])
        klant_id = c1.selectbox("Klant", [k.id for k in klanten],
                                format_func=lambda i: next(k.naam for k in klanten if k.id == i), key="klant_concept")
        upload = c2.file_uploader("Asperion-balans (PDF of Excel)", type=["pdf", "xlsx"], key="balans")
        datum = c3.date_input("Datum samenstelling", date.today(), format="DD-MM-YYYY")

        if st.button("Maak concept", type="primary", disabled=upload is None, icon=":material/play_arrow:"):
            klant = Klant(KLANTEN_ROOT / klant_id)
            pad = tijdelijk_bestand(upload)
            st.session_state.pop("concept", None)
            st.session_state.pop("definitief", None)
            with st.status("Concept maken...", expanded=True) as status:
                def voortgang(stap, detail=""):
                    if detail:
                        status.write(f"**{STAPPEN[stap]}**: {detail}")
                    else:
                        status.update(label=f"{STAPPEN[stap]}...")
                try:
                    st.session_state.concept = maak_concept(klant, pad, datum, kantoor, ai=ai, voortgang=voortgang)
                    status.update(label="Concept klaar", state="complete", expanded=False)
                except KoppelFout as e:
                    status.update(label="Niet-gekoppelde grootboekrekeningen", state="error")
                    st.error("Deze grootboekrekeningen zijn nieuw en nog niet gekoppeld aan een post:\n\n"
                             + "\n".join(f"- {r['nr']} {r['naam']} ({r['groep']})" for r in e.rekeningen)
                             + "\n\nVul een Claude API-sleutel in (Claude stelt dan een koppeling voor) of koppel ze "
                               "handmatig in het tabblad **Koppelingen en klantgegevens**.")
                except (InvoerFout, DossierFout, RenderFout, AIFout) as e:
                    status.update(label="Concept maken mislukt", state="error")
                    st.error(str(e))

        concept = st.session_state.get("concept")
        if concept and concept.klant.id == klant_id:
            st.divider()
            tel = concept.status_telling
            kop1, kop2 = st.columns([3, 2])
            kop1.subheader(f"Concept jaarrekening {concept.jaar} · {concept.meta['bedrijf']}")
            samenvatting = f"{tel['OK']} controles OK, {tel['LET OP']} LET OP, {tel['FOUT']} FOUT"
            if tel["FOUT"]:
                kop2.error(samenvatting, icon=":material/error:")
            elif tel["LET OP"]:
                kop2.warning(samenvatting, icon=":material/warning:")
            else:
                kop2.success(samenvatting, icon=":material/check_circle:")
            for w in concept.waarschuwingen:
                st.warning(w)

            c, p = concept.c, concept.p
            m = st.columns(4)
            for kol, (post, lab) in zip(m, [("omzet", "Omzet"), ("resultaat", "Resultaat"),
                                             ("ev", "Eigen vermogen"), ("activa", "Balanstotaal")]):
                kol.metric(lab, euro(c[post]), f"{fmt(c[post] - p[post])} t.o.v. {concept.jaar - 1}")

            links, rechts = st.columns([1, 1], gap="large")
            with links:
                st.markdown("#### Aandachtspunten")
                for a in concept.aandachtspunten:
                    with st.container(border=True):
                        st.badge(a["ernst"], color=ERNST_KLEUR[a["ernst"]])
                        st.markdown(f"**{a['titel']}**  \n{a['toelichting']}")
                        if a.get("onbekende_bedragen"):
                            st.caption(f":orange[Bedrag(en) {', '.join(a['onbekende_bedragen'])} komen niet uit de "
                                       "berekening: controleer deze tekst.]")
                if concept.aandachtspunten_bron == "ai":
                    tok = sum(g.input_tokens + g.output_tokens for g in concept.gebruik)
                    st.caption(f"Geschreven door Claude Opus 5.5 · {tok:,} tokens · ${concept.kosten_usd:.4f}{SCHATTING}"
                               .replace(",", "."))
                else:
                    st.caption("Opgesteld door de code (zonder AI).")
                if concept.ai_koppelingen:
                    st.info("**Nieuwe grootboekrekeningen, gekoppeld door Claude** (keur ze goed in het tabblad "
                            "Koppelingen):\n\n" + "\n".join(
                                f"- {k['nr']} {k['naam']} → {k['label']} (zekerheid {k['zekerheid']})"
                                for k in concept.ai_koppelingen))
            with rechts:
                st.markdown("#### Controles")
                st.dataframe(controletabel(concept.controles), hide_index=True, width="stretch",
                             column_config={"Detail": st.column_config.TextColumn(width="large")})
                d1, d2 = st.columns(2)
                d1.download_button("Download concept (PDF)", concept.pdf.read_bytes(), concept.pdf.name,
                                   "application/pdf", type="primary", width="stretch",
                                   icon=":material/download:")
                d2.download_button("Download controlerapport", concept.rapport.read_bytes(), concept.rapport.name,
                                   "text/markdown", width="stretch")

            with st.expander("Voorbeeld van het concept", expanded=False, icon=":material/visibility:"):
                toon_pdf(concept.pdf, "concept")

            st.markdown("#### Vaststellen")
            bestaat = concept.klant.jaarrekening(concept.jaar)
            if bestaat and bestaat.get("status") == "vastgesteld":
                st.warning(f"Er is al een vastgestelde jaarrekening {concept.jaar}; vaststellen overschrijft die.")
            akkoord = st.checkbox("Ik heb het concept en de aandachtspunten beoordeeld")
            if tel["FOUT"]:
                st.caption("Vaststellen kan pas als er geen controles op FOUT staan.")
            if st.button("Vaststellen", disabled=not akkoord or tel["FOUT"] > 0, icon=":material/verified:"):
                st.session_state.definitief = stel_vast(concept)
            definitief = st.session_state.get("definitief")
            if definitief:
                st.success(f"Vastgesteld. De cijfers {concept.jaar} zijn opgeslagen als vergelijkende cijfers voor "
                           f"{concept.jaar + 1}.")
                st.download_button("Download definitieve jaarrekening (PDF)", definitief.read_bytes(), definitief.name,
                                   "application/pdf", icon=":material/download:")


# --------------------------------------------------------------------------
# Tab 2: Nieuwe klant
# --------------------------------------------------------------------------
with tab_nieuw:
    st.markdown("Upload de **jaarrekening van vorig jaar** (PDF). Claude leest die eenmalig uit; de code controleert "
                "daarna dat elk bedrag letterlijk in de PDF staat en dat alle totalen optellen.")
    if "melding_nieuw" in st.session_state:
        st.success(st.session_state.pop("melding_nieuw"), icon=":material/check_circle:")
    oud = st.file_uploader("Jaarrekening vorig jaar (PDF)", type=["pdf"], key="oude_jaarrekening")
    if st.button("Jaarrekening inlezen", type="primary", disabled=oud is None or ai is None,
                 icon=":material/upload_file:"):
        try:
            with st.spinner("Claude leest de jaarrekening..."):
                st.session_state.inlezing = lees_vorige_jaarrekening(tijdelijk_bestand(oud), ai)
        except AIFout as e:
            st.error(str(e))
    if ai is None:
        st.caption("Hiervoor is een Claude API-sleutel nodig (zijbalk).")

    inl = st.session_state.get("inlezing")
    if inl:
        st.divider()
        g1, g2, g3, g4 = st.columns([3, 1, 2, 2])
        inl.naam = g1.text_input("Bedrijfsnaam", inl.naam)
        inl.jaar = int(g2.number_input("Boekjaar", 2000, 2100, inl.jaar or date.today().year - 1))
        inl.gegevens["vestigingsplaats"] = g3.text_input("Vestigingsplaats", inl.gegevens["vestigingsplaats"])
        inl.gegevens["kvk"] = g4.text_input("KvK-nummer", inl.gegevens["kvk"])

        labels = labels_voor({"leasecontracten": inl.leasecontracten})
        df = pd.DataFrame([{"Post": labels.get(k, k), "Sleutel": k, "Bedrag": v} for k, v in inl.posten.items()])
        links, rechts = st.columns([1, 1], gap="large")
        with links:
            st.markdown("#### Uitgelezen bedragen")
            st.caption("Corrigeer zo nodig een bedrag en klik op *Opnieuw controleren*.")
            bewerkt = st.data_editor(df, hide_index=True, width="stretch", height=420,
                                     disabled=["Post", "Sleutel"], key="posten_editor")
        with rechts:
            st.markdown("#### Controles")
            st.dataframe(controletabel(inl.controles), hide_index=True, width="stretch")
            if inl.leasecontracten:
                st.markdown("**Leasecontracten**: " + ", ".join(s["label"] for s in inl.leasecontracten.values()))
            st.text_area("Afschrijvingstekst", "\n".join(inl.gegevens["afschrijvingen_tekst"]), key="afschr_tekst")
        if st.button("Opnieuw controleren"):
            inl.posten.update({r["Sleutel"]: int(r["Bedrag"]) for _, r in bewerkt.iterrows()})
            hercontroleer(inl)
            st.rerun()
        toch = st.checkbox("Toch opslaan ondanks FOUT", disabled=inl.fouten == 0) if inl.fouten else False
        if st.button("Klant opslaan", type="primary", disabled=inl.fouten > 0 and not toch, icon=":material/save:"):
            inl.posten.update({r["Sleutel"]: int(r["Bedrag"]) for _, r in bewerkt.iterrows()})
            inl.gegevens["afschrijvingen_tekst"] = [r for r in st.session_state.afschr_tekst.splitlines() if r.strip()]
            try:
                k = bewaar_als_klant(inl, root=KLANTEN_ROOT)
                st.session_state.pop("inlezing")
                st.session_state.melding_nieuw = (f"Klant **{k.naam}** opgeslagen met de jaarrekening {inl.jaar} als "
                                                  "vergelijkende cijfers. Kies de klant in **Maak concept**.")
                st.rerun()
            except (FileExistsError, ValueError) as e:
                st.error(str(e))


# --------------------------------------------------------------------------
# Tab 3: Koppelingen en klantgegevens
# --------------------------------------------------------------------------
with tab_koppel:
    if not klanten:
        st.info("Nog geen klanten.")
    else:
        klant_id3 = st.selectbox("Klant", [k.id for k in klanten], key="klant_koppel",
                                 format_func=lambda i: next(k.naam for k in klanten if k.id == i))
        klant = Klant(KLANTEN_ROOT / klant_id3)
        mapping = klant.mapping
        labels = labels_voor(mapping)
        opties_keys = BALANS_REKENINGPOSTEN + list(mapping.get("leasecontracten", {})) + WV_REKENINGPOSTEN
        optie_tekst = {k: f"{labels.get(k, k)} [{k}]" for k in opties_keys}

        ai_v = mapping.get("ai_voorstellen", {})
        open_ai = [nr for nr, v in ai_v.items() if not v.get("goedgekeurd")]
        if open_ai:
            st.warning(f"{len(open_ai)} koppeling(en) van Claude wachten op goedkeuring (bovenaan de tabel).")

        rijen = []
        for nr, post in mapping["rekeningen"].items():
            v = ai_v.get(nr)
            rijen.append({"Rekening": nr,
                          "Naam": mapping.get("rekeningnamen", {}).get(nr, v["naam"] if v else ""),
                          "Post": optie_tekst.get(post, post),
                          "Door Claude": v is not None,
                          "Goedgekeurd": (v or {}).get("goedgekeurd", True),
                          "Toelichting Claude": (v or {}).get("toelichting", "")})
        df = pd.DataFrame(rijen).sort_values(["Goedgekeurd", "Rekening"], key=lambda s: s if s.name == "Goedgekeurd"
                                             else s.str.zfill(6))
        st.markdown("#### Koppelingen grootboekrekening → post")
        bewerkt = st.data_editor(
            df, hide_index=True, width="stretch", height=460, key=f"koppel_{klant_id3}",
            disabled=["Rekening", "Naam", "Door Claude", "Toelichting Claude"],
            column_config={"Post": st.column_config.SelectboxColumn(options=list(optie_tekst.values()), width="large"),
                           "Toelichting Claude": st.column_config.TextColumn(width="medium")})
        if st.button("Koppelingen opslaan", type="primary", icon=":material/save:"):
            terug = {t: k for k, t in optie_tekst.items()}
            for _, r in bewerkt.iterrows():
                mapping["rekeningen"][r["Rekening"]] = terug.get(r["Post"], r["Post"])
                if r["Rekening"] in ai_v:
                    ai_v[r["Rekening"]]["goedgekeurd"] = bool(r["Goedgekeurd"])
                    ai_v[r["Rekening"]]["post"] = mapping["rekeningen"][r["Rekening"]]
            mapping["ai_voorstellen"] = ai_v
            klant.bewaar_mapping(mapping)
            st.session_state.pop(f"koppel_{klant_id3}", None)   # tabel opnieuw opbouwen (volgorde verandert)
            st.session_state.melding_koppel = "Koppelingen opgeslagen."
            st.rerun()
        if "melding_koppel" in st.session_state:
            st.success(st.session_state.pop("melding_koppel"), icon=":material/check_circle:")

        st.markdown("#### Klantgegevens")
        geg = klant.gegevens
        with st.form(f"klant_{klant_id3}"):
            f1, f2, f3 = st.columns(3)
            naam = f1.text_input("Bedrijfsnaam", geg.get("naam", ""))
            plaats = f2.text_input("Vestigingsplaats", geg.get("vestigingsplaats", ""))
            kvk = f3.text_input("KvK-nummer", geg.get("kvk", ""))
            afschr = st.text_area("Afschrijvingstekst (één regel per regel in de jaarrekening)",
                                  "\n".join(geg.get("afschrijvingen_tekst", [])))
            lease = st.text_area("Teksten leasecontracten (alinea's gescheiden door een lege regel)",
                                 "\n\n".join(geg.get("lease_teksten", [])), height=150)
            if st.form_submit_button("Klantgegevens opslaan"):
                klant.bewaar_gegevens({**geg, "naam": naam, "vestigingsplaats": plaats, "kvk": kvk,
                                       "afschrijvingen_tekst": [r for r in afschr.splitlines() if r.strip()],
                                       "lease_teksten": [a.strip() for a in lease.split("\n\n") if a.strip()]})
                st.success("Klantgegevens opgeslagen.")
        jaren = klant.jaren()
        st.caption(f"Jaarrekeningen in het dossier: {', '.join(map(str, jaren)) or 'geen'}.")


# --------------------------------------------------------------------------
# AI-gebruik in de zijbalk (als laatste, zodat de verzoeken van deze run meetellen)
# --------------------------------------------------------------------------
if kosten_plek is not None:
    t = ai.totaal
    with kosten_plek:
        c1, c2 = st.columns(2)
        c1.metric("AI-kosten sessie", f"${t['kosten_usd']:.4f}")
        c2.metric("Tokens", f"{t['input_tokens'] + t['output_tokens']:,}".replace(",", "."))
        st.caption(f"{t['verzoeken']} verzoek(en): {t['input_tokens']:,} input, {t['output_tokens']:,} output{SCHATTING}"
                   .replace(",", "."))
