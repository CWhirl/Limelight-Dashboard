# Project-Limelight v1 B2 fields: original-code crosswalk

| B2 field | Created from source | What it actually represents | Dashboard |
|---|---|---|---|
| `node_id` | AutoNet local identity passed to `local_observation` | Hardware-derived trusted node ID | Roster-mapped name; ID also visible |
| `data.hostname` | `os.uname().nodename` | Raspberry Pi OS hostname (can change) | Identity comparison |
| `slot`, `observed_utc` | `local_observation` | Scheduled UTC hour / actual sample timestamp | Hourly coverage and time filters |
| `severity`, `data.errors` | `local_observation` | Collector alert severity and explicit reason codes | Warning breakdown |
| `data.cpu_temperature_c` | `/sys/class/thermal/thermal_zone0/temp` | SoC temperature | CPU chart |
| `data.thermal_previous_hour.{samples,min_c,max_c,mean_c}` | SQLite 5-minute temp samples | Previous-hour observed temperature summary | Mean chart when supplied |
| `data.energy.telemetry_status` | `limelight_daemon.cycle` | `ok` if VE.Direct sample exists, else `unavailable` | Energy availability |
| `data.energy.battery.battery_voltage_v` | VE.Direct `V` via `PowerSample.json()` | Measured MPPT-reported battery voltage | Bank voltage |
| `data.energy.battery.solar_voltage_v` | VE.Direct `VPV` | PV voltage | PV voltage |
| `data.energy.battery.solar_power_w` | VE.Direct `PPV` | Solar production (W) | Solar chart |
| `data.energy.battery.mppt_output_current_a` | VE.Direct `I` | Charger output current, not net battery current | Charge current |
| `data.energy.battery.load_current_a` | VE.Direct `IL` | Reported load current when available | Load current |
| `data.energy.battery.charger_state` | VE.Direct `CS` | Numeric Victron charger state | Charger state |
| `data.energy.battery.charger_error` | VE.Direct `ERR` | Numeric Victron error | Charger error |
| `data.autonet.selected_gateway.{node_id,address,label}` | `limelight_autonet_agent.execute_cycle` | Elected gateway (NOT verified physical adjacency) | Gateway selection |
| `data.autonet.peers` | `compact_status` of AutoNet `network-status.json` | Reachable peer *status advertisements* up to 40, possibly empty | Peer list/topology when present |
| `data.autonet.last_updated` | AutoNet Unix-seconds status timestamp | Age of status when snapshotted | Freshness chart |
| `data.power.{armed,arm_pending,phase,action,radio_should_be_on}` | `limelight_runtime` status | Controller intent/state | Power state charts |
| `data.health.{failed_services,pi_throttle_flags,recent_system_errors}` | `health_snapshot()` | Read-only system diagnostics | Health table |
| `kind=weekly`, `data.count,warnings,errors,coverage_hex,...` | `archive_old` → `item_from_archive` | Historical compressed report coverage, **not** live data | Weekly archive tab |

## What cannot be plotted from these cycle objects

* Physical PowerBeam port/link electrical state, RF RSSI, ping latency/loss, or actual transferred bytes: these are not captured in the logged `compact_status` projection.
* True battery state-of-charge, with only MPPT voltage and output current available.
* Actual connected-path graph when peers lists are empty or not reported; the selected gateway is a software choice, not a verified link.

The dashboard does not install modifications to the source repository or the Pi deployment. Any future additional telemetry should be planned against the existing fleet logger and validated before deployment, not inferred by the visualizer.
