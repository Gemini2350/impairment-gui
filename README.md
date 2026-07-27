# Impairment Control GUI

Web GUI to toggle **packet loss** and **jitter** on two devices (LEG A / LEG B)
via `PUT /api/p_cap/instances/1/mode_command`.

Body format: `[mode, [min_delay_ns, jitter, packetloss]]` — e.g. `[1,[1000000,0,0.0007]]`

## Run with Docker

```bash
docker compose up -d --build
```

Then open `http://<docker-host>:9080` in your browser.

- **Main page:** ping status of both legs (online/offline), plus one on/off switch each
  for packet loss and jitter. ON sends the configured value, OFF sends 0 in that
  position — the other value and the min delay are left untouched.
- **Setup:** IPs of the two legs, min. delay (ns), jitter and packet loss values
  (used when ON) and, per function, which p_cap instance they target (0, 1 or both);
  plus API path and mode. Saving sends the current target state to both legs.
  Commands go to every targeted instance, with a value set only if its function
  is ON and targets that instance (0 otherwise).
- **Resend values:** sends the current target state to the device again
  (e.g. after a device reboot).
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
