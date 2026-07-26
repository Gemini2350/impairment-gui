# Impairment Control GUI

Web-GUI zum Schalten von **Packetloss** und **Jitter** auf zwei Geräten (LEG A / LEG B)
über `PUT /api/p_cap/instances/1/mode_command`.

Body-Format: `[mode, [min_delay_ns, jitter, packetloss]]` — z.B. `[1,[1000000,0,0.0007]]`

## Start mit Docker

```bash
docker compose up -d --build
```

Danach im Browser: `http://<docker-host>:8080`

- **Hauptseite:** Ping-Status beider Legs (online/offline), je ein On/Off-Schalter für
  Packetloss und Jitter. ON sendet den konfigurierten Wert, OFF sendet 0 an dieser
  Position — der jeweils andere Wert und der Min-Delay bleiben dabei erhalten.
- **Setup:** IPs der beiden Legs, Min. Delay (ns), Jitter- und Packetloss-Wert (bei ON),
  API-Pfad und Mode. Beim Speichern wird der aktuelle Sollzustand an beide Legs gesendet.
- **Werte erneut senden:** schickt den aktuellen Sollzustand nochmals ans Gerät
  (z.B. nach einem Geräte-Neustart).

Die Konfiguration inkl. Schaltzustand liegt persistent in `./data/config.json`.

## Ohne Docker (zum Testen)

```bash
pip install flask
python3 app.py
```
