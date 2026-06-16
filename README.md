# StratoPi HSO

High-altitude balloon stack: Pi telemetry (`stratopi.local`) + Windows Ground Control.

## Ground Control on a new laptop (from GitHub)

```powershell
git clone https://github.com/Starman42X/StratoPi-HSO.git
cd StratoPi-HSO
powershell -ExecutionPolicy Bypass -File setup_laptop.ps1
.\run_ground_control.ps1
```

Open **http://localhost:5001**

Optional COM port: `.\run_ground_control.ps1 -Serial COM3`

### Not in Git (create or copy manually)

| File | Why |
|------|-----|
| `ground_station/lora_crypto.json` | AES key must match the Pi — copy from your other laptop or sync from Pi in the UI |
| `ground_station/gs_alerts.json` | Local milestones / Pi URL — created from `.example` by `setup_laptop.ps1` |

### Phone map (PC mobile hotspot)

- `http://whereami.local:5001/whereami`
- `http://192.168.137.1:5001/whereami` (fallback)

### Pi web

- `http://stratopi.local:8080` (home Wi‑Fi)
- `http://192.168.4.1:8080` (Pi hotspot **Strato-HSO**)

### Standalone .exe (no Python)

```powershell
cd ground_station
.\build_exe.ps1
```

Copy the `ground_station\dist\` folder to the other PC.