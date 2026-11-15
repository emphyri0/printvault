# Printvault

Een kleine webapp om je 3D-prints bij te houden: foto's, modelbestanden (3mf, stl, …), filament, printtijd, instellingen en notities.

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
```
