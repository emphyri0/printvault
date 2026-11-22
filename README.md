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
- Live statuskaart zoals het printerscherm: fase, voortgang, eindtijdstip, temperaturen, snelheid, ventilatoren en actief AMS-slot
- Temperatuur- en faseverloop per print als grafiek op de detailpagina
- Live camerabeeld van de printer op de statuskaart, en automatisch een camerafoto bij de print als die klaar is of mislukt

## Live koppeling (MQTT)

Vul bij **Printers → Bewerk** het IP-adres, serienummer en de access code in en vink *Live koppeling aan* aan. De service `printvault-mqtt` leest de instellingen elke minuut opnieuw in; meer printers toevoegen kan zonder herstart.

- Verbinding: `mqtts://<ip>:8883`, gebruiker `bblp`, wachtwoord = access code, TLS zonder certificaatcontrole. Alleen subscribe op `device/<serienummer>/report`; er wordt niets naar de printer gestuurd.
- `PREPARE`/`RUNNING` → print met status *bezig*; `FINISH` → *gelukt* met eindtijd en duur; `FAILED` → *mislukt* met de foutcode van de printer.
- Herstart de listener tijdens een print, dan wordt het bestaande *bezig*-record hervat (herkend aan `task_id`). Zag de listener het einde van een print niet en start er een nieuwe, dan krijgt de oude status *onbekend* met een notitie (*opnieuw* is alleen voor prints die je bewust opnieuw wilt maken).
- Live status (percentage, resterende tijd, laag) staat alleen in het geheugen van de listener en is via `/api/live` op te vragen.
- Status *onbekend*: de listener zag het einde van de print niet (bv. omdat hij niet verbonden was). Controleer zelf hoe de print afliep.
- AMS: per unit hoogstens één meting per 10 minuten in `ams_readings` (vochtigheid in % als de firmware `humidity_raw` stuurt, anders Bambu's niveau 1–5, en temperatuur).
- Logboek: `docker compose logs -f printvault-mqtt`

## Camera

Printers met een IP-adres en access code (X1, P2S, H2D: RTSPS op poort 322) krijgen een camerabeeld. Op de printer moet *LAN-liveview* aan staan.

- Op de statuskaart: knop **📷 Camera**; bij **Printers** een link die het beeld in een nieuw tabblad opent.
- `/printers/<id>/camera.mjpg`: live beeld als MJPEG (± 5 fps, max. 1280 breed; aan te passen met `CAMERA_FPS` en `CAMERA_WIDTH`). ffmpeg in de container zet de stream om. Per printer is er één verbinding, gedeeld door alle kijkers. Die stopt 15 seconden nadat de laatste kijker weg is; een verborgen tabblad stopt de stream ook.
- `/printers/<id>/snapshot.jpg`: één beeld in volle resolutie. Het beeld wordt ~20 s per printer bewaard (ook een mislukte poging), zodat meerdere tabbladen de printer niet extra belasten; loopt de live stream, dan komt het beeld daaruit.
- Op de homepage toont de kaart van een print met status *bezig* zonder eigen omslag (een thumbnail telt niet; die is de reserve als er geen camerabeeld is) het camerabeeld (label *live*), elke 30 s ververst. Meldt de listener dat de print niet meer loopt, dan volgt nog één laatste beeld (label *einde*); na herladen staat er de eindfoto die de listener aan de print hing.
- Gaat een print live naar *klaar* of *mislukt*, dan hangt de listener er een camerafoto aan (`camera-gelukt-…jpg` / `camera-mislukt-…jpg`). Een zelf gekozen omslag of een eerder toegevoegde eigen foto blijft de omslag; een slicer-/MakerWorld-thumbnail wordt door de camerafoto vervangen (de thumbnail blijft wel bij de foto's). Lukt het beeld niet, dan staat dat in de log en loopt de rest gewoon door. Een einde dat de listener pas na een herstart opmerkt, krijgt geen foto.
- De access code komt niet in logs of foutmeldingen terecht.

## Statuskaart en tijdreeks

- De statuskaart toont wat het printerscherm toont: fase in leesbare tekst (`stg_cur`), plaat x van y, %, laag, resterende tijd en eindtijdstip, nozzle-/bed-/kamertemperatuur (huidig/doel; de P2S heeft geen doel voor de kamer), snelheidsprofiel, ventilatoren (onderdeel, aux, kamer, heatbreak) en het actieve AMS-slot met kleur. Links staat het camerabeeld (als de print geen eigen omslag heeft), anders de omslag of de printerfoto.
- Velden die ontbreken (ander model, oudere firmware) vallen gewoon weg. De vertaling van de ruwe velden staat in `bambu_status.py`.
- Bij de start van een print vult de listener in het printrecord in: nozzlediameter, nozzletype (bv. *Gehard staal (HS01)*), plaat (`device.plate.cur_id`, bv. `P0101`; de bron heeft daar geen leesbare naam voor), plaatnummer en aantal platen, snelheidsprofiel en of spaghettidetectie en eerstelaaginspectie aanstonden. Alleen velden die nog leeg zijn; wat je zelf invulde (of wat uit een 3mf kwam) blijft. Komt het volledige rapport pas later binnen, dan worden de lege velden alsnog ingevuld.
- Tijdreeks: per print hoogstens één meetpunt per minuut (temperaturen, fase, %, laag) in `print_samples`, plus een punt bij elke faseovergang en bij het einde. Op de detailpagina als grafiek met fasebalk, crosshair en tabel.
- Filter op nozzlediameter in het overzicht.
- Volledig MQTT-rapport bekijken: `data/mqtt-rapport-p2s.json` (access code eruit gefilterd; niet in git).

## Foutcodes

De teksten (Nederlands, Engels als terugval, P2S-varianten waar die bestaan), wiki-links en faseteksten (`CURRENT_STAGE_IDS` uit `pybambu/const.py` met de vertalingen, gelezen met `ast.literal_eval`, niet uitgevoerd) staan in `bambu_errors_data.json`. Eigen Nederlandse faseteksten in `bambu_status.STAGE_TEXT` gaan voor. Bron: de foutcodelijsten van [ha-bambulab](https://github.com/greghesp/ha-bambulab) (`scripts/hms_error_text/`, MIT), die zelf van Bambu Lab komen (`e.bambulab.com`, zoals Bambu Studio) en van de [HMS-wiki](https://wiki.bambulab.com/en/hms/home). De gebruikte commit staat in `_source` in het bestand.

```bash
python3 scripts/update_bambu_errors.py             # tabel opnieuw genereren (laatste main)
python3 scripts/update_bambu_errors.py --ref <sha> # of een vaste versie
```

Daarna opnieuw bouwen (`docker compose up -d --build`). Eigen teksten en de voorgestelde faalreden per code staan in `bambu_errors.py` (`PRINT_ERRORS_EXTRA`, `HMS_CODES_EXTRA`, `FAIL_REASONS_BY_CODE`) en worden door het script niet overschreven. Alleen HMS-codes hebben een eigen wikipagina; voor print_error-codes en HMS-codes zonder bekende pagina linkt Printvault naar de HMS-startpagina.

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
