# Printvault

Een kleine webapp om je 3D-prints bij te houden: foto's, modelbestanden (3mf, stl, …), filament, printtijd, instellingen en notities.

## Functies

- Prints met foto's, modelbestanden (3mf, stl, step, obj, gcode), filament, printtijd, instellingen en notities
- Printers met foto, en per print kiezen op welke printer hij gemaakt is
- Importeren vanaf MakerWorld: plak een modellink en titel, ontwerper, cover, filament, gewicht en printtijd worden ingevuld (het 3mf-bestand voeg je zelf toe)
- Live koppeling met Bambu Lab-printers via MQTT (alleen lezen, cloudmodus mag aan blijven): prints worden automatisch aangemaakt en afgesloten, met AMS-slots, en de homepage toont een live statuskaart
- Foutcodes (print_error en HMS) als leesbare tekst, met een voorgestelde faalreden bij mislukte prints
- AMS-vochtigheid en -temperatuur per unit, met een verloop van de laatste 24 uur op de statuskaart
- Onderhoudstaken per printer met een interval in printuren en/of dagen
- Meldingen via ntfy: print klaar, print mislukt, onderhoud vervallen (elk apart aan/uit)

## Live koppeling (MQTT)

Vul bij **Printers → Bewerk** het IP-adres, serienummer en de access code in en vink *Live koppeling aan* aan. De service `printvault-mqtt` leest de instellingen elke minuut opnieuw in; meer printers toevoegen kan zonder herstart.

- Verbinding: `mqtts://<ip>:8883`, gebruiker `bblp`, wachtwoord = access code, TLS zonder certificaatcontrole. Alleen subscribe op `device/<serienummer>/report`; er wordt niets naar de printer gestuurd.
- `PREPARE`/`RUNNING` → print met status *bezig*; `FINISH` → *gelukt* met eindtijd en duur; `FAILED` → *mislukt* met de foutcode van de printer.
- Herstart de listener tijdens een print, dan wordt het bestaande *bezig*-record hervat (herkend aan `task_id`). Zag de listener het einde van een print niet en start er een nieuwe, dan krijgt de oude status *onbekend* met een notitie (*opnieuw* is alleen voor prints die je bewust opnieuw wilt maken).
- Live status (percentage, resterende tijd, laag) staat alleen in het geheugen van de listener en is via `/api/live` op te vragen.
- Status *onbekend*: de listener zag het einde van de print niet (bv. omdat hij niet verbonden was). Controleer zelf hoe de print afliep.
- AMS: per unit hoogstens één meting per 10 minuten in `ams_readings` (vochtigheid in % als de firmware `humidity_raw` stuurt, anders Bambu's niveau 1–5, en temperatuur).
- Logboek: `docker compose logs -f printvault-mqtt`

## Foutcodes

De vertaling staat in `bambu_errors.py`: `PRINT_ERRORS` (notatie `0300-400C`) en `HMS_CODES` (notatie `0700_2000_0002_0001`), telkens `code: (tekst, faalreden of None)`. De tabel bevat bewust alleen codes waarvan de betekenis vaststaat; voeg er gerust aan toe. Een onbekende code wordt getoond als code met een link naar de Bambu-wiki (`WIKI_URL`).

## Onderhoud en meldingen

- **Printers → Onderhoud**: taken met een interval in printuren en/of dagen; de taak vervalt bij wat het eerst bereikt wordt. Printuren = printtijd van de prints die na *laatst uitgevoerd* klaar waren (geplande en lopende prints tellen niet). Bij de eerste start met deze versie krijgen bestaande printers een set standaardtaken voor de P2S.
- **Instellingen**: ntfy-server, topic, optioneel access token, en per soort melding aan/uit. Met *Testmelding sturen* controleer je de instellingen.
- De meldingen verstuurt de listener (`printvault-mqtt`). Onderhoud wordt elke minuut nagekeken; een vervallen taak geeft één melding, en opnieuw pas nadat ze uitgevoerd is en weer vervalt.
- Geen melding voor een einde dat de listener pas na een herstart opmerkt.

## Starten

```bash
cp .env.example .env              # vul SECRET_KEY in
mkdir -p data
docker compose up -d --build
```

Daarna draait de app op http://<vm-ip>:8100. Hij is ook bereikbaar via Tailscale.

## Data

Alles staat in `./data`: de SQLite-database `printvault.db` en de uploads in `uploads/`. Maak een back-up van die map en je hebt alles.

## Lokaal ontwikkelen

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
python app.py                     # http://127.0.0.1:8100
python mqtt_listener.py           # tweede terminal; live status op http://127.0.0.1:8101/live
python -m unittest discover -s tests
```
