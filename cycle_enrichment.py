"""Extract only observed facts from Limelight v1 cycle JSON envelopes.

A requested radio state is not a measured relay or physical link state.
A selected gateway is not proof of reachability or a peer adjacency.
"""
from __future__ import annotations

from collections.abc import Mapping


def _mapping(v):
    return v if isinstance(v, Mapping) else {}


def _as_text(v):
    return v[:500] if isinstance(v, str) else None


def _as_bool(v):
    return v if isinstance(v, bool) else None


def _as_float(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and -1e6 <= float(v) <= 1e6:
        return float(v)
    return None


def extract_cycle(payload):
    """Extract cycle metadata, gateway *selection*, health text and commanded power intent."""
    if not isinstance(payload, Mapping):
        return {}
    data = _mapping(payload.get('data'))
    if not data:
        return {}
    autonet = _mapping(data.get('autonet'))
    gateway = _mapping(autonet.get('selected_gateway'))
    power = _mapping(data.get('power'))
    health = _mapping(data.get('health'))
    energy = _mapping(data.get('energy'))
    out = {'is_cycle': bool('slot' in payload or 'power' in data)}
    for key, value in {
        'record_kind': payload.get('kind'),
        'cycle_slot': payload.get('slot'),
        'hostname': data.get('hostname'),
        'selected_gateway_id': gateway.get('node_id'),
        'selected_gateway_label': gateway.get('label'),
        'selected_gateway_address': gateway.get('address'),
        'power_action': power.get('action'),
        'power_phase': power.get('phase'),
        'power_profile_hash': power.get('profile_sha256'),
        'power_battery_guard': power.get('battery_guard'),
        'energy_telemetry_status': energy.get('telemetry_status'),
        'health_failed_services': health.get('failed_services'),
        'health_recent_errors': health.get('recent_system_errors'),
        'health_throttle_flags': health.get('pi_throttle_flags'),
    }.items():
        val = _as_text(value)
        if val is not None:
            out[key] = val
    for key, value in {
        'power_armed': power.get('armed'),
        'power_arm_pending': power.get('arm_pending'),
        'radio_should_be_on': power.get('radio_should_be_on'),
    }.items():
        val = _as_bool(value)
        if val is not None:
            out[key] = val
    if isinstance(data.get('errors'), list):
        # The repo's local_observation builds these cause codes before assigning severity.
        out['cycle_errors'] = ' | '.join(str(code)[:150] for code in data['errors'][:20])
        out['cycle_error_count'] = len(data['errors'])
    last_update = _as_float(autonet.get('last_updated'))
    if last_update and 1_000_000_000 < last_update < 5_000_000_000:
        out['autonet_last_updated_unix'] = last_update
    peers = autonet.get('peers')
    if isinstance(peers, list):
        out['autonet_peers_list_recorded'] = True
        out['autonet_peer_count'] = len(peers)
    if isinstance(power.get('relay_gpio_bcm'), int):
        out['power_relay_gpio_bcm'] = power['relay_gpio_bcm']
    cpu = _as_float(data.get('cpu_temperature_c'))
    if cpu is not None:
        out['cpu_temperature_c'] = cpu
    thermal = _mapping(data.get('thermal_previous_hour'))
    for source, target in (
        ('samples', 'thermal_samples'), ('min_c', 'thermal_min_c'),
        ('max_c', 'thermal_max_c'), ('mean_c', 'thermal_mean_c')
    ):
        val = _as_float(thermal.get(source))
        if val is not None:
            out[target] = val
    # Distinguish explicitly null battery status from absent field.
    if 'battery' in energy:
        out['battery_present'] = energy['battery'] is not None
        out['battery_explicit_null'] = energy['battery'] is None
    if out.get('is_cycle'):
        out['is_cycle'] = True
    return out
