# Impairment Control GUI

Web GUI to impair two ST 2110 legs (LEG A / LEG B) for failover testing:
packet loss via Arista storm-control, jitter via a single p_cap impairment
device that legs are routed through by VLAN switching, plus PTP GM port
shutdowns and a Hue power plug.

Device command: `PUT /api/p_cap/instances/{instance}/mode_command` with body
`[mode, [min_delay_ns, jitter, packetloss]]` — e.g. `[1,[1000000,200000,0]]`

## Run with Docker

```bash
docker compose up -d --build
```

Then open `http://<docker-host>:9080` in your browser.

- **Packet loss (per leg):** ON sets `storm-control multicast level pps
  <kpps*1000>` on the leg's switch port, OFF removes the limit
  (1 stream ≈ 109 kpps; value configured in setup).
- **Jitter (global):** Off / LEG A / LEG B. The selected leg's switch port
  moves into the jitter VLAN (default 666) where the single p_cap device sits,
  the other one goes back to its normal VLAN (defaults: LEG A → 1, LEG B → 2).
  The active state and per-port VLANs are read back from the switch. The device
  itself gets `[mode,[min_delay, jitter, 0]]` on the configured instance(s);
  its ping status, address and a *Resend values* button live on the jitter card.
- **Setup:** device address, min delay, jitter value, p_cap instance, packet
  loss kpps, jitter/normal VLANs, Arista switch + port interfaces and labels,
  Hue bridge. Saving re-sends the device values and the storm-control state.
- **Main PTP:** shuts the PTP grandmaster port on an Arista switch down / up
  via eAPI, with live admin/link status.
- **Power:** switches a Philips Hue smart plug on/off via the local bridge API.
  Pairing (press the bridge link button, then click *Pair with bridge*) and a
  plug/light list are built into the setup page.

The configuration including switch state is persisted in `./data/config.json`.

## Without Docker (for testing)

```bash
pip install flask
python3 app.py
```
