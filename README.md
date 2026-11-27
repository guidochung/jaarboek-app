# Jaarboek-app

**Eén venster (`app.py`):**
1. Upload het **jaarverslag van vorig jaar** (PDF, uit het rapportageprogramma van het kantoor).
2. Upload de **nieuwe Asperion-balans** (PDF of Excel).
3. Klik op **Maak jaarverslag**.

### Hoe het werkt (`jaarboek/doorrol.py`)
Het oude rapport wordt het sjabloon, ongeacht de klant of de opbouw (eenmanszaak, BV, meerjarenoverzichten, fiscale positie, ...):
1. **Opbouw lezen, zonder AI** (`rapportlezer.py`): pagina's, teksten, lijnen, logo's en tabellen met hun kolommen en jaren.
2. **Elke regel met een bedrag koppelen** aan Asperion:
   - de code zoekt eerst zelf (één Asperion-bedrag geeft de waarde en de omschrijving lijkt);
   - de rest stelt Claude Opus 5.5 voor als formule.
3. **Bewijzen:** elke formule, toegepast op de Asperion-kolom van vorig jaar, moet exact het bedrag uit het oude rapport geven (2 euro marge, nooit met omgedraaid teken). Lukt dat niet, dan krijgt Claude één herkansing met feedback; daarna wordt het FOUT.
4. **Extra vangnetten:**
   - tabellen moeten optellen zoals in het oude rapport;
   - dezelfde regel moet overal hetzelfde bedrag hebben;
   - elke Asperion-rekening moet in het rapport terugkomen;
   - het rapport en de balans moeten van dezelfde klant en het juiste jaar zijn.
5. **Doorschuiven:** elke tabel schuift één jaar op (2 kolommen blijft 2, 5 blijft 5); het oudste jaar valt weg.
6. **Opnieuw opmaken:** exact zoals het origineel (zelfde pagina's, posities, lijnen en logo's), met nieuwe bedragen, jaartallen en datums. Het nieuwe rapport is zelf weer in te lezen voor volgend jaar.

### Uitvoer
- het concept met CONCEPT-watermerk
- een versie zonder watermerk
- `controlerapport_<jaar>.md` (per regel: oud bedrag, bewijs, nieuw bedrag, formule)
- `koppelingen.json`

### Kosten
Getest op E. van Manen Beheer 2 BV (15 pagina's, 60 regels): $0,08 per jaarverslag. 33 regels vond de code zelf; voor 27 was Claude nodig.

De uitgebreide versie met klantbeheer (`app_uitgebreid.py`) gebruikt nog de vaste MISTO-opbouw.

Thema en kleuren staan in `.streamlit/config.toml` (licht en donker). Start de app vanuit de app-map, anders wordt dat thema niet geladen.

Hiervoor is een Claude API-sleutel nodig, omdat Claude het oude jaarverslag uitleest. Een concept kost 3 verzoeken: uitlezen, nieuwe rekeningen koppelen en aandachtspunten. Upload je hetzelfde oude jaarverslag nog een keer in dezelfde sessie, dan wordt het niet opnieuw uitgelezen.

De uitgebreide versie met klantbeheer, vaste dossiers en een koppelingenscherm staat in `app_uitgebreid.py` (`streamlit run app_uitgebreid.py`). De rest van deze README beschrijft die versie.

---

Maakt met één knop een **concept-jaarrekening** uit de Asperion-balans (PDF of Excel), met controles en aandachtspunten voor de accountant.

- Alle bedragen komen uit code en worden **letterlijk uit Asperion overgenomen**. De AI typt nooit een bedrag in de jaarrekening.
- **Claude Opus 5.5** (`claude-opus-5-5`) doet alleen tekstwerk, en zo zuinig mogelijk:
  - aandachtspunten schrijven: één klein verzoek per concept
  - koppelvoorstellen voor nieuwe grootboekrekeningen
  - eenmalig de jaarrekening van vorig jaar uitlezen bij een nieuwe klant
- Zonder API-sleutel werkt alles behalve die AI-onderdelen. De aandachtspunten komen dan uit de code.

## Starten

```bash
cd ~/Desktop/Jaarboek\ app && streamlit run app.py
```

Vul de Claude API-sleutel in de zijbalk in. Demo zonder sleutel en zonder kosten, met een nagebootste Claude:

```bash
cd ~/Desktop/Jaarboek\ app && JAARBOEK_NEP_AI=1 streamlit run app.py
```

Vereist: Python 3.10+, de pakketten uit `requirements.txt` en Google Chrome (voor het maken van de PDF).

## Werkwijze in de app

1. **Maak concept**:
   - kies de klant, upload de Asperion-balans (PDF of Excel) en klik op **Maak concept**
   - je krijgt de PDF met CONCEPT-watermerk, de kerncijfers, de controles (OK / LET OP / FOUT), de aandachtspunten en een controlerapport
   - **Vaststellen** maakt de definitieve PDF zonder watermerk en bewaart de cijfers als vergelijkende cijfers voor volgend jaar
2. **Nieuwe klant**:
   - upload de jaarrekening van vorig jaar (PDF); Claude leest die uit
   - de code controleert dat elk bedrag letterlijk in de PDF staat en dat de totalen optellen
   - corrigeer zo nodig en sla de klant op
3. **Koppelingen en klantgegevens**: koppelingen van grootboekrekeningen nakijken, en voorstellen van Claude goedkeuren of wijzigen.

Nieuwe grootboekrekeningen worden automatisch gekoppeld via een voorstel van Claude. Ze staan op **LET OP** tot ze zijn goedgekeurd.

## Omzet naar Asperion (tweede onderdeel, zijbalk)

Kassa-omzet van één maand → één kasboekboeking als FMUTAC-CSV voor Asperion.

- **Lezen**: elk kassasysteem levert iets anders. Vaste lezers (`jaarboek/kassalezers.py`) herkennen het formaat aan de titel van het rapport:
  - dag- en productrapport (Excel, Engels of Nederlands)
  - Restaurant summary month + category report (PDF)
  - Omzetlijst (PDF)
  - Boekhouding overzicht (PDF)

  Een onbekend formaat kan Claude lezen (`jaarboek/kassa_claude.py`, knop **Lees met Claude**, ca. $0,05 – $0,10). Daarna moet elk overgenomen bedrag letterlijk in het rapport staan; anders FOUT.
- **Tussenformaat** (`Kassaomzet` in `jaarboek/omzet.py`): omzet per categorie (of per btw-tarief), betalingen per betaalwijze, fooi, overige inkomsten en het btw-overzicht van de kassa. Boeking, controles en CSV zijn voor elk kassasysteem gelijk.
- **Koppeling per klant**: categorie (+ tarief) → grootboekrekening, betaalwijze → tegenrekening, dagboek, boekstukprefix en boekingsstijl:
  - **uitgebreid**: elke betaalwijze apart (Bar Bellini)
  - **compact**: tegenkant per tegenrekening, overal dezelfde omschrijving

  Lokaal bewaard in `klanten/omzet/<klant>.json`; op de server download en upload je hem.
- **Btw**: per regel bedrag incl. btw × tarief / (100 + tarief), zoals het kantoor en Asperion rekenen. Wijkt de btw van de kassa meer dan € 0,05 af, dan staat dat als LET OP in de controles.
- **Controles** (FOUT blokkeert de download): omzet per tarief tegen het btw-overzicht van de kassa, elke regel een rekening, btw-codes, sluit op nul. Op LET OP staan het verschil (betalingen − omzet − fooi − overige) en posten die niet geboekt worden.

## Kosten (Claude Opus 5.5: $4 / $20 per miljoen input-/output-tokens)

| Taak | Wanneer | Schatting |
|---|---|---|
| Aandachtspunten | elk concept | ca. $0,01 – $0,05 |
| Koppelvoorstellen | alleen bij nieuwe grootboekrekeningen | ca. $0,01 |
| Vorige jaarrekening uitlezen | eenmalig per nieuwe klant | ca. $0,05 – $0,30 |

De app toont per concept en per sessie het echte gebruik. Het wordt ook per klant gelogd in `klanten/<klant>/ai_gebruik.jsonl`. Echte cijfers meten:

```bash
cd ~/Desktop/Jaarboek\ app && python3 -P proefrun_api.py
```

Zet hiervoor eerst `ANTHROPIC_API_KEY`. Het script werkt op een kopie van de dossiers.

## Opdrachtregel

```bash
cd ~/Desktop/Jaarboek\ app && python3 -P build_jaarrekening.py --klant misto_interiors --balans ~/Downloads/Balans_WV_Report_9_11_2026.xlsx
```

Opties: `--ai` (Claude gebruiken), `--datum 2026-11-16`, `--vaststellen`.

## Tests

```bash
cd ~/Desktop/Jaarboek\ app && python3 -P -m pytest tests -q
```

De 36 tests dekken o.a. het doorrollen van een BV-rapport met 5-jarige meerjarenoverzichten (`tests/test_doorrol.py`, met de echte Claude-formules als opname) en:
- de **golden test**: MISTO 2025 moet exact gelijk blijven aan de goedgekeurde PDF
- PDF- en Excel-invoer die gelijk moeten zijn
- verlies met het juiste teken
- de AI-onderdelen met een nagebootste client, waaronder het afvangen van verzonnen bedragen, weigeringen en de fallback
- de volledige route voor een nieuwe klant: oude PDF → klant → concept, met dezelfde uitkomst als de referentie
- het opstarten van de app

## Mappen

| Pad | Inhoud |
|---|---|
| `app.py` | Eén venster: oud jaarverslag + nieuwe balans → nieuw jaarverslag |
| `app_uitgebreid.py` | Uitgebreide app met klantbeheer en koppelingen |
| `jaarboek/` | Code: `eenvoudig` (de ene stap), `inlezen`, `berekenen`, `signalen`, `ai`, `onboarding`, `pipeline`, `render`, `rapport`, `dossier`; omzet: `omzet`, `kassalezers`, `kassa_claude` |
| `omzet_koppeling.json` | Standaardkoppeling kassa-omzet (categorieën, tegenrekeningen, btw-codes) |
| `templates/jaarrekening.html.j2` | Opmaak en standaardteksten van het kantoor |
| `kantoor.json` | Kantoorgegevens, drempels voor signalen en de AI-effort |
| `standaard_mapping.json` | Standaardkoppeling voor nieuwe klanten (Asperion-rekeningschema) |
| `klanten/<klant>/` | `klant.json`, `mapping.json`, `jaarrekening_<jaar>.json`, `output/<jaar>/` |
| `tests/` | Tests, testbestanden, golden referentie, nagebootste Claude |

## Regels die in de code vastliggen

- Bedragen worden letterlijk overgenomen zoals Asperion ze toont. Wat Asperion niet toont, wordt opgeteld. Er worden geen afrondingscorrecties gedaan. Verschillen van €1-2 door afronding in Asperion staan als LET OP in de controles.
- Kortlopend deel van leaseverplichtingen = aflossing in het boekjaar (methode van de vorige jaarrekening).
