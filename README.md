# Printvault

Een kleine webapp om je 3D-prints bij te houden: foto's, modelbestanden (3mf, stl, …), filament, printtijd, instellingen en notities.

## Functies

- Prints met foto's, modelbestanden (3mf, stl, step, obj, gcode), filament, printtijd, instellingen en notities
- Printers met foto, en per print kiezen op welke printer hij gemaakt is
- Importeren vanaf MakerWorld: plak een modellink en titel, ontwerper, cover, filament, gewicht en printtijd worden ingevuld (het 3mf-bestand voeg je zelf toe)
- Live koppeling met Bambu Lab-printers via MQTT (alleen lezen, cloudmodus mag aan blijven): prints worden automatisch aangemaakt en afgesloten, met AMS-slots, en de homepage toont een live statuskaart

## Live koppeling (MQTT)

Vul bij **Printers → Bewerk** het IP-adres, serienummer en de access code in en vink *Live koppeling aan* aan. De service `printvault-mqtt` leest de instellingen elke minuut opnieuw in; meer printers toevoegen kan zonder herstart.

- Verbinding: `mqtts://<ip>:8883`, gebruiker `bblp`, wachtwoord = access code, TLS zonder certificaatcontrole. Alleen subscribe op `device/<serienummer>/report`; er wordt niets naar de printer gestuurd.
- `PREPARE`/`RUNNING` → print met status *bezig*; `FINISH` → *gelukt* met eindtijd en duur; `FAILED` → *mislukt* met de foutcode van de printer.
- Herstart de listener tijdens een print, dan wordt het bestaande *bezig*-record hervat (herkend aan `task_id`). Zag de listener het einde van een print niet en start er een nieuwe, dan krijgt de oude status *onbekend* met een notitie (*opnieuw* is alleen voor prints die je bewust opnieuw wilt maken).
- Live status (percentage, resterende tijd, laag) staat alleen in het geheugen van de listener en is via `/api/live` op te vragen.
- Logboek: `docker compose logs -f printvault-mqtt`

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
