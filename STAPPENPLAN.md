# Stappenplan Jaarboek-app (Streamlit)

> **Status 17-11-2026: fase 1 t/m 5 gebouwd en getest**, behalve de echte API-proefrun. Die vraagt een API-sleutel; zie `proefrun_api.py`.
>
> - 21 geautomatiseerde tests slagen.
> - De app is in de browser getest met een nagebootste Claude: concept (PDF en Excel), voorbeeld, vaststellen, nieuwe klant inlezen en opslaan, koppelingen goedkeuren.
>
> Gekozen bij de open beslissingen:
> 1. Nieuwe rekeningen: het AI-voorstel wordt automatisch toegepast en staat op LET OP tot het is goedgekeurd.
> 2. Word-uitvoer: later.
> 3. De app draait lokaal.

**Doel:** één knop **"Maak concept"**. De accountant kiest een klant, uploadt de Asperion-balans (PDF of Excel) en krijgt een concept-jaarrekening met controles en aandachtspunten.

**Uitgangspunten**
- Alle bedragen komen uit code en worden letterlijk uit Asperion overgenomen. De AI typt nooit een bedrag.
- Model: Claude Opus 5.5 (`claude-opus-5-5`), zo zuinig mogelijk met tokens.
- PDF- en Excel-invoer leveren exact dezelfde uitkomst.

---

## Hoe de knop werkt

```
[Maak concept]
   1. Balans inlezen (PDF/Excel)          code
   2. Dossier vorig jaar + koppelingen     code
   3. Nieuwe rekeningen?  -- nee -->       (geen AI nodig)
                          -- ja  -->       AI: koppelvoorstel (klein verzoek)
   4. Jaarrekening berekenen               code
   5. Controles                            code
   6. Aandachtspunten                      code berekent de signalen, AI schrijft de tekst (één verzoek)
   7. PDF maken (met CONCEPT-watermerk)    code
```

**Waarom dit zuinig is:** de AI krijgt nooit de hele balans of PDF te zien. Code maakt een compacte samenvatting (kerncijfers, grote mutaties, controle-uitkomsten, ongeveer 1-2K tokens) en Claude krijgt alleen die. Bij een klant zonder nieuwe rekeningen is dat **één AI-verzoek per concept**.

**Geschatte kosten per concept:** ongeveer $0,02 – $0,10 (Opus 5.5, effort `medium`). In de app meten we het echte gebruik.

---

## Fase 1: Code opsplitsen (fundament)

1. `build_jaarrekening.py` opsplitsen in een pakket `jaarboek/`:
   - `inlezen.py`: Asperion PDF/Excel naar dezelfde structuur
   - `berekenen.py`: posten, totalen, eigen vermogen, lease
   - `controles.py`: controles met status OK / LET OP / FOUT
   - `signalen.py`: grote mutaties en ontbrekende posten, als compacte JSON
   - `render.py`: HTML naar PDF
   - `ai.py`: alle Claude-aanroepen op één plek
2. Klantdossiers per map: `klanten/<klant>/mapping.json`, `jaarrekening_<jaar>.json`, `output/`.
3. De CLI en de bestaande tests (`test_pdf_excel_gelijk.py`) blijven werken.
4. **Golden test:** de MISTO-jaarrekening 2025 moet na de opsplitsing exact hetzelfde blijven.

## Fase 2: Streamlit-scherm (zonder AI)

1. **Zijbalk:** Claude API-sleutel (zoals in de thesis-app), kantoorinstellingen (naam, plaats, ondertekenaar).
2. **Hoofdscherm:**
   - klant kiezen (keuzelijst uit `klanten/`)
   - balans uploaden (PDF of Excel)
   - datum samenstelling (standaard vandaag)
   - knop **Maak concept**
3. **Voortgang** met `st.status`: elke stap krijgt een vinkje.
4. **Resultaat:**
   - downloadknop voor de PDF, plus een voorbeeldweergave
   - kerncijfers (omzet, resultaat, eigen vermogen, balanstotaal) tegenover vorig jaar
   - controletabel met kleuren (groen OK, oranje LET OP, rood FOUT)
5. **Knop "Vaststellen":** maakt de PDF zonder watermerk en slaat de cijfers op als dossier voor volgend jaar.

## Fase 3: AI-deel (zuinig)

1. **Aandachtspunten-verzoek:** één `messages.create` met een vaste, korte systeemprompt en de compacte JSON van `signalen.py`. Uitvoer via structured output: een lijst `{punt, toelichting, ernst}`. Zo zijn er geen lange teksten en hoeven we niets te parsen.
2. **Koppelvoorstel-verzoek:** alleen als er nieuwe grootboekrekeningen zijn. Invoer: nummer en naam van de nieuwe rekening plus de lijst van mogelijke posten. Uitvoer: de voorgestelde post met een zekerheid. Code controleert of de post bestaat.
3. **Zuinig met tokens:**
   - effort `medium` (instelbaar per kantoor: low / medium / high)
   - een lage `max_tokens` per verzoek
   - geen PDF's of volledige balansen naar de AI, alleen samenvattingen
   - de systeemprompt vast houden, zodat caching werkt als die groot genoeg is
4. **Tokenteller in de app:** toon per concept de input- en output-tokens en de kosten, zoals de live tokenweergave in de thesis-app. Elke run wordt gelogd.
5. **Zonder API-sleutel** werkt alles behalve de AI-aandachtspunten. De aandachtspunten uit code blijven gewoon zichtbaar.

## Fase 4: Nieuwe klant toevoegen

1. Scherm "Nieuwe klant": de jaarrekening van vorig jaar uploaden (PDF).
2. Claude leest de PDF eenmalig uit naar een dossier-JSON. Dit is het enige grote verzoek, ongeveer $0,30 – $0,50 per klant.
3. Code controleert of het uitgelezen dossier optelt en sluit. Bij fouten toont het scherm wat er mis is.
4. Daarna de koppelingen vaststellen. Dat gebeurt automatisch door de vorig-jaarkolom van de Asperion-balans te laten aansluiten op het dossier.

## Fase 5: Testen en pilot

1. Tests: PDF = Excel, golden test MISTO, controles. AI-aanroepen worden in de tests nagebootst (gemockt), zodat ze niets kosten.
2. Eén echte proefrun met de API om het tokengebruik te meten.
3. Pilot bij het kantoor met 5-10 klanten. Meten: tijdwinst per jaarrekening, kosten per concept en wat de accountant nog handmatig aanpast.

---

## Open beslissingen

1. **Nieuwe rekeningen:** stopt de knop en vraagt hij om goedkeuring, of past hij het AI-voorstel automatisch toe en markeert het als LET OP?
2. **Word-uitvoer (.docx)** al in de eerste versie, of later?
3. **Waar draait de app:** lokaal op een laptop van het kantoor, of online?
