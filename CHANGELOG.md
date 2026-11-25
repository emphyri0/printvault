# Changelog

## v1.0.0 – 2026-11-25

First release: a self-hosted archive for 3D prints, with a live, read-only connection to Bambu Lab printers. Only the P2S has been tested on hardware; other models are expected to work.

- **Prints:** photos, model files, material, filament colours, weight, print time, slicer settings, tags and notes, with search and filters.
- **Import** from a MakerWorld link or a sliced 3MF/G-code file.
- **Live connection** (MQTT, read-only): prints are created and closed automatically; the home page shows the running print with progress, temperatures, fans, filament and AMS humidity.
- **Camera:** live image, refreshed image on the running print's card, and an automatic photo when a print finishes or fails.
- **Temperature and stage chart** per print.
- **Error codes** as readable text with a wiki link and a suggested fail reason.
- **Maintenance** tasks per printer, by print hours and/or days.
- **Notifications** via ntfy: print finished, print failed, maintenance due.
- **Print again** in Bambu Studio or through MakerWorld.
- **Thumbnails:** scaled-down WebP versions of photos, without metadata; originals are kept unchanged.
- **Security:** host name and cross-site request checks. There is no login: only run it on a trusted network.
- **Interface:** dark, image-first and usable on a phone, without requests to other sites.
