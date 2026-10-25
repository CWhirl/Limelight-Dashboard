"""Conservative telemetry and peer extraction from Limelight JSON and text.

Do not infer physical link state, battery SoC, or missing measurements.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping


def norm(v):
    return re.sub(r"[^a-z0-9]+", "_", str(v).lower()).strip("_")


def numeric(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if abs(float(v)) < 1e14 else None
    if isinstance(v, str):
        m = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?)\s*(?:V|mV|W|mW|A|mA|°C|C|dBm|ms|%)?\s*", v, re.I)
        return float(m.group(1)) if m else None
    return None


EXACT = {
    'battery_voltage_v': ('battery_voltage_v', 'battery_v', 'battery_voltage', 'batteryvoltage', 'battery_volts', 'vbat'),
    'solar_power_w': ('solar_power_w', 'solar_w', 'solar_power', 'pv_power_w', 'generation_w', 'pv_power', 'panel_power_w'),
    'pv_voltage_v': ('pv_voltage_v', 'solar_voltage_v', 'panel_voltage_v', 'pv_voltage', 'solar_voltage'),
    'charge_current_a': ('charge_current_a', 'mppt_current_a', 'mppt_output_current_a', 'solar_current_a', 'charge_current', 'charger_current_a'),
    'load_current_a': ('load_current_a', 'load_current'),
    'temperature_c': ('temperature_c', 'battery_temperature_c', 'mppt_temperature_c', 'temp_c'),
    'peer_count': ('peer_count', 'peers_count', 'neighbor_count', 'neighbour_count', 'reachable_peers', 'connected_peer_count'),
    'transferred_files': ('transferred_files', 'files_transferred', 'transferred', 'files_uploaded'),
    'transferred_bytes': ('transferred_bytes', 'bytes_transferred', 'uploaded_bytes', 'bytes_uploaded'),
    'remaining_files': ('remaining_files', 'files_remaining'),
    'charger_error': ('charger_error', 'charger_error_code', 'mppt_error', 'error_code'),
    'network_rssi_dbm': ('rssi_dbm', 'signal_dbm', 'signal_strength_dbm'),
    'latency_ms': ('latency_ms', 'rtt_ms', 'ping_ms', 'round_trip_ms'),
    'packet_loss_pct': ('packet_loss_pct', 'loss_pct', 'packet_loss_percent'),
    'witty_input_voltage_v': ('witty_input_voltage_v', 'witty_v_in_v'),
    'witty_output_current_a': ('witty_output_current_a', 'witty_i_out_a'),
}
# Raw VE.Direct readings are in millivolts / milliamps (except PPV in W).
# Short keys are ONLY accepted under a clearly identified Victron/MPPT context.
VE_DIRECT = {'v': ('battery_voltage_v', .001), 'vpv': ('pv_voltage_v', .001),
             'ppv': ('solar_power_w', 1), 'i': ('charge_current_a', .001),
             'il': ('load_current_a', .001), 'err': ('charger_error', 1)}


def leaves(value, prefix='', depth=0):
    if depth > 10:
        return
    if isinstance(value, Mapping):
        for key, sub in value.items():
            path = f'{prefix}.{key}' if prefix else str(key)
            if isinstance(sub, (Mapping, list)):
                yield from leaves(sub, path, depth + 1)
            else:
                yield path, sub
    elif isinstance(value, list):
        for i, sub in enumerate(value[:250]):
            yield from leaves(sub, f'{prefix}[]', depth + 1)


def _under_mppt(path):
    segs = [norm(seg) for seg in path.split('.')]
    return any(seg in ('mppt', 'victron', 've_direct', 'vedirect', 've_direct_raw', 'solar_controller') for seg in segs[:-1])


def metrics_from_payload(payload):
    pairs = list(leaves(payload))
    values = {}
    # Prioritize explicit-unit, full semantic keys, then abbreviated raw MPPT keys.
    for metric, aliases in EXACT.items():
        for alias in aliases:
            match = next(((p, v) for p, v in pairs if norm(p.split('.')[-1]) == alias and numeric(v) is not None), None)
            if match:
                values[metric] = numeric(match[1])
                break
    for path, value in pairs:
        key = norm(path.split('.')[-1])
        if not _under_mppt(path) or key not in VE_DIRECT:
            continue
        metric, scale = VE_DIRECT[key]
        n = numeric(value)
        if n is not None and metric not in values:
            values[metric] = round(n * scale, 6)
    # Named VE.Direct keys at a top-level VE.Direct export.
    top_keys = set(payload) if isinstance(payload, Mapping) else set()
    if {'V', 'VPV', 'PPV'}.issubset(top_keys):
        for key, (metric, scale) in VE_DIRECT.items():
            raw_key = key.upper()
            if metric not in values and raw_key in payload:
                n = numeric(payload[raw_key])
                if n is not None:
                    values[metric] = round(n * scale, 6)
    # Recognize Witty supply data without confusing it with the bank voltage.
    for path, value in pairs:
        if 'witty' in norm(path) or 'wp5' in norm(path):
            k = norm(path.split('.')[-1])
            dest = {'v_in': 'witty_input_voltage_v', 'vin': 'witty_input_voltage_v',
                    'i_out': 'witty_output_current_a', 'iout': 'witty_output_current_a'}.get(k)
            if dest and dest not in values:
                n = numeric(value)
                if n is not None:
                    values[dest] = n
    return values


def _id_from_peer(p):
    if isinstance(p, str):
        return p.strip()[:120]
    if isinstance(p, Mapping):
        for key in ('node_id', 'peer_id', 'hostname', 'node_name', 'id', 'name', 'address'):
            if p.get(key):
                return str(p[key])[:120]
    return None


def _find_peer_list(obj, depth=0):
    if not isinstance(obj, Mapping) or depth > 6:
        return None
    for k, v in obj.items():
        if norm(k) in ('peers', 'neighbors', 'neighbours', 'connected_peers', 'babel_neighbors', 'mesh_peers'):
            if isinstance(v, (list, dict)):
                return v
        if isinstance(v, Mapping):
            found = _find_peer_list(v, depth+1)
            if found is not None:
                return found
    return None


def topology_from_payload(payload):
    result = {}
    peers = _find_peer_list(payload)
    if isinstance(peers, list):
        result['peer_count'] = len(peers)
        found = [_id_from_peer(p) for p in peers]
        result['peer_ids'] = '|'.join(dict.fromkeys(x for x in found if x))
    elif isinstance(peers, dict):
        # May be mapping of peer IDs to detail dicts.
        result['peer_count'] = len(peers)
        found = [k for k in peers if isinstance(k, str)]
        result['peer_ids'] = '|'.join(found[:100])
    for path, val in leaves(payload):
        key = norm(path.split('.')[-1])
        if key in ('babel_running', 'internet_verified', 'gateway_verified', 'starlink_verified') and isinstance(val, bool):
            result[key] = val
    return result


TEXT_PATTERNS = {
    'battery_voltage_v': [r'\bBattery\s*(?:bank\s*)?(?:voltage|V)\s*[:=]\s*([\d.]+)\s*V?\b',
                          r'\bbattery_voltage_v\s*[:=]\s*([\d.]+)'],
    'solar_power_w': [r'\bSolar\s*(?:generation|power)\s*[:=]\s*([\d.]+)\s*W?\b',
                      r'\b(?:PV|solar)\s*power\s*[:=]\s*([\d.]+)\s*W?\b'],
    'pv_voltage_v': [r'\b(?:Solar|PV|panel)\s*voltage\s*[:=]\s*([\d.]+)\s*V?\b'],
    'charge_current_a': [r'\b(?:MPPT|charge|charger)\s*current\s*[:=]\s*([+-]?[\d.]+)\s*A?\b'],
    'peer_count': [r'\b(?:peer|neighbor|neighbour)\s*count\s*[:=]\s*(\d+)\b'],
}


def metrics_from_text(message):
    result = {}
    for metric, patterns in TEXT_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, message, re.I)
            if match:
                result[metric] = float(match.group(1))
                break
    # Tokenized VE.Direct lines (V and VPV in mV, current in mA), only when labeled.
    if 'VE.Direct' in message or 'Victron' in message:
        pairs = dict(re.findall(r'\b(VPV|PPV|IL|ERR|V|I)\s*[:=]\s*([+-]?\d+(?:\.\d+)?)', message))
        for key, val in pairs.items():
            metric, scale = VE_DIRECT[key.lower()]
            result.setdefault(metric, float(val) * scale)
    return result


def compact_field_paths(payload, max_paths=400):
    """Schema only; NEVER include values, credentials, or free-text messages."""
    types = {}
    for path, val in leaves(payload):
        if len(types) >= max_paths:
            break
        type_label = 'boolean' if isinstance(val, bool) else 'numeric' if isinstance(val, (int, float)) else 'null' if val is None else 'string'
        types[path] = type_label
    return types
