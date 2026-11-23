"""Vaste structuur van de jaarrekening (het model van het kantoor) en hulpfuncties."""
from __future__ import annotations

LABELS = {
    "mva_inventaris": "Inventaris",
    "mva_vervoer": "Automobielen en overige transportmiddelen",
    "mva": "Materiële vaste activa",
    "deb_debiteuren": "Debiteuren",
    "debiteuren": "Debiteuren",
    "ov_vooruitbetalingen": "Vooruitbetalingen",
    "overige_vorderingen": "Overige vorderingen",
    "vorderingen": "Vorderingen",
    "bank_rc": "Rekening-courant bank",
    "bank_spaar": "(Bedrijfs)spaarrekening",
    "tegoeden": "Tegoeden op bankrekeningen",
    "krp_stortingen": "Stortingen onderweg",
    "kruisposten": "Kruisposten",
    "liquide": "Liquide middelen",
    "vlottend": "Vlottende activa",
    "activa": "Totaal activa",
    "kapitaal": "Kapitaal",
    "ev": "Eigen vermogen",
    "lt_fin_lease": "Financial Leaseverplichtingen",
    "lt": "Langlopende schulden",
    "kt_fin_lease": "Financial leaseverplichtingen",
    "crediteuren": "Crediteuren",
    "belastingen": "Belastingen en premies sociale verzekeringen",
    "btw_omzetbelasting": "Omzetbelasting",
    "overlopende": "Overlopende passiva",
    "otb_nog_te_betalen": "Nog te betalen kosten",
    "otb_vooruitontvangen": "Vooruitontvangen bedragen",
    "kt": "Kortlopende schulden",
    "passiva": "Totaal passiva",
    "omzet_omzet": "Omzet",
    "omzet": "Omzet",
    "inkopen": "Inkopen",
    "kostprijs": "Kostprijs van de omzet",
    "brutomarge": "Brutomarge",
    "afschrijvingen": "Afschrijvingen",
    "afschr_mva": "Afschrijvingen materiële vaste activa",
    "afschr_transport": "Transport- en vervoermiddelen",
    "afschr_inventaris": "Bedrijfsinventaris",
    "pers_overig": "Overige personeelskosten",
    "pers_uitzend": "Uitzendkrachten",
    "huisvesting": "Huisvestingskosten",
    "huis_huur": "Betaalde huur",
    "huis_schoonmaak": "Schoonmaakkosten",
    "verkoop": "Verkoopkosten",
    "verk_reclame": "Reclame- en advertentiekosten",
    "verk_representatie": "Representatiekosten",
    "verk_relatiegeschenken": "Relatiegeschenken",
    "auto": "Autokosten",
    "auto_brandstof": "Brandstofkosten auto's",
    "auto_onderhoud": "Reparatie en onderhoud auto's",
    "auto_mrb": "Motorrijtuigenbelasting",
    "auto_operational_lease": "Operational leasing auto's",
    "auto_overig": "Overige autokosten",
    "kantoor": "Kantoorkosten",
    "kant_benodigdheden": "Kantoorbenodigdheden",
    "kant_telefoon": "Telefoon en internet",
    "algemeen": "Algemene kosten",
    "alg_administratie": "Administratiekosten",
    "alg_verzekeringen": "Verzekeringen",
    "alg_kleine_aansch": "Kleine aanschaffingen",
    "alg_abonnementen": "Abonnementen/contributies",
    "alg_betalingsverschillen": "Betalingsverschillen",
    "alg_werkkleding": "Werkkleding",
    "alg_overig": "Overige algemene kosten",
    "totaal_kosten": "Totaal kosten",
    "bedrijfsresultaat": "Bedrijfsresultaat",
    "rentebaten": "Rentebaten en soortgelijke opbrengsten",
    "rb_bank": "Rentebaten bankrekeningen",
    "rentelasten": "Rentelasten en soortgelijke kosten",
    "rl_financieringen": "Rentelasten financieringen",
    "financieel": "Financiële baten en lasten",
    "resultaat": "Resultaat",
    # rekening-niveau (alleen voor koppelingen)
    "ev_rekeningen": "Eigen vermogen (kapitaal, privé, resultaat)",
}

# W&V hoofdpost -> detailregels (volgorde = volgorde in de jaarrekening)
WV_STRUCTUUR = {
    "omzet": ["omzet_omzet"],
    "kostprijs": ["inkopen"],
    "afschrijvingen": ["afschr_transport", "afschr_inventaris"],
    "pers_overig": ["pers_uitzend"],
    "huisvesting": ["huis_huur", "huis_schoonmaak"],
    "verkoop": ["verk_reclame", "verk_representatie", "verk_relatiegeschenken"],
    "auto": ["auto_brandstof", "auto_onderhoud", "auto_mrb", "auto_operational_lease", "auto_overig"],
    "kantoor": ["kant_benodigdheden", "kant_telefoon"],
    "algemeen": ["alg_administratie", "alg_verzekeringen", "alg_kleine_aansch", "alg_abonnementen",
                 "alg_betalingsverschillen", "alg_werkkleding", "alg_overig"],
    "rentebaten": ["rb_bank"],
    "rentelasten": ["rl_financieringen"],
}
KOSTEN_POSTEN = ["afschrijvingen", "pers_overig", "huisvesting", "verkoop", "auto", "kantoor", "algemeen"]

# Posten op rekeningniveau waaraan een grootboekrekening gekoppeld kan worden
# (leasecontracten komen per klant uit de mapping).
BALANS_REKENINGPOSTEN = [
    "mva_inventaris", "mva_vervoer", "deb_debiteuren", "ov_vooruitbetalingen", "bank_rc", "bank_spaar",
    "krp_stortingen", "ev_rekeningen", "crediteuren", "btw_omzetbelasting", "otb_nog_te_betalen",
    "otb_vooruitontvangen",
]
WV_REKENINGPOSTEN = [k for regels in WV_STRUCTUUR.values() for k in regels]

# Saldo credit (passiva / opbrengsten): teken omdraaien. Leaseposten komen erbij uit de mapping.
CREDIT_POSTEN = {
    "ev_rekeningen", "crediteuren", "btw_omzetbelasting",
    "otb_nog_te_betalen", "otb_vooruitontvangen", "omzet_omzet", "rb_bank",
}

# Alle posten van een volledige jaarrekening (sleutels in het dossier), excl. leasecontracten
ALLE_POSTEN = [
    "mva_inventaris", "mva_vervoer", "mva", "deb_debiteuren", "debiteuren", "ov_vooruitbetalingen",
    "overige_vorderingen", "vorderingen", "bank_rc", "bank_spaar", "tegoeden", "krp_stortingen",
    "kruisposten", "liquide", "vlottend", "activa", "kapitaal", "ev", "lt_fin_lease", "lt",
    "kt_fin_lease", "crediteuren", "btw_omzetbelasting", "belastingen", "otb_nog_te_betalen",
    "otb_vooruitontvangen", "overlopende", "kt", "passiva",
    "omzet_omzet", "omzet", "inkopen", "kostprijs", "brutomarge",
    "afschr_transport", "afschr_inventaris", "afschr_mva", "afschrijvingen",
    "pers_uitzend", "pers_overig", "huis_huur", "huis_schoonmaak", "huisvesting",
    "verk_reclame", "verk_representatie", "verk_relatiegeschenken", "verkoop",
    "auto_brandstof", "auto_onderhoud", "auto_mrb", "auto_operational_lease", "auto_overig", "auto",
    "kant_benodigdheden", "kant_telefoon", "kantoor",
    "alg_administratie", "alg_verzekeringen", "alg_kleine_aansch", "alg_abonnementen",
    "alg_betalingsverschillen", "alg_werkkleding", "alg_overig", "algemeen",
    "totaal_kosten", "bedrijfsresultaat", "rb_bank", "rentebaten", "rl_financieringen", "rentelasten",
    "financieel", "resultaat",
]

MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus",
           "september", "oktober", "november", "december"]


def fmt(n) -> str:
    """Bedrag in Nederlandse notatie zonder decimalen: 12345 -> '12.345', -14 -> '-14'."""
    if n is None:
        return ""
    n = int(n)
    s = f"{abs(n):,}".replace(",", ".")
    return f"-{s}" if n < 0 else s


def datum_nl(d) -> str:
    """datetime.date -> '16 november 2026'."""
    return f"{d.day} {MAANDEN[d.month - 1]} {d.year}"


def labels_voor(mapping: dict) -> dict:
    """LABELS aangevuld met de leasecontracten van deze klant."""
    lab = dict(LABELS)
    for post, spec in mapping.get("leasecontracten", {}).items():
        lab[post] = spec["label"]
        lab[spec["lt_post"]] = spec["label"]
    return lab


def credit_posten(mapping: dict) -> set:
    return CREDIT_POSTEN | set(mapping.get("leasecontracten", {}))
