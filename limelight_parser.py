"""Read-only parsers for heterogeneous Project Limelight diagnostic exports."""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import zipfile
from datetime import datetime
from typing import Any

import pandas as pd
from telemetry_enrichment import metrics_from_payload, topology_from_payload, metrics_from_text
from cycle_enrichment import extract_cycle

ALLOWED_EXTENSIONS = (".log", ".txt", ".json", ".jsonl", ".ndjson", ".csv", ".gz", ".zip")
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 800
MAX_ROWS_PER_FILE = 60_000
MAX_DEPTH = 2

DATE_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[,.]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)"
)
NODE_RE = re.compile(r"\b((?:lime(?:node|base))[-_]?\d+|limelight[-_]?(?:node|base)[-_]?\d+)\b", re.I)
NODE_ID_RE = re.compile(r"(?<![a-fA-F0-9])([a-fA-F0-9]{12})(?![a-fA-F0-9])")
LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b", re.I)
EVENT_RE = re.compile(
    r"\b(CONTROLLER_STARTED|WINDOW_OPEN|WINDOW_CLOSED|POWERBEAM_ON|POWERBEAM_OFF|"
    r"RELAY_(?:ON|OFF)|GPIO\d+_(?:ON|OFF)|SESSION_[A-Z_]+|SYNC_RESULT|"
    r"NEXT_EVENT|PEER_[A-Z_]+|TRANSFER_[A-Z_]+|UPLOAD_[A-Z_]+|"
    r"WITTY_[A-Z_]+|SHUTDOWN_[A-Z_]+|STARTUP_[A-Z_]+|"
    r"STATUS_SNAPSHOT|BATTERY_[A-Z_]+|MPPT_[A-Z_]+|WINDOW_SYNC_SUSPENDED)\b"
)
KV_RE = re.compile(r"(?<!\w)([A-Za-z][\w.-]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)")

ALIASES = {
    "battery_voltage_v": {"battery_voltage_v", "battery_v", "battery_voltage", "vbat", "batteryvoltage"},
    "solar_power_w": {"solar_power_w", "solar_w", "solar_power", "ppv", "pv_power_w", "generation_w"},
    "pv_voltage_v": {"pv_voltage_v", "solar_voltage_v", "vpv", "panel_voltage_v"},
    "charge_current_a": {"charge_current_a", "mppt_current_a", "solar_current_a", "charge_current", "charger_current_a"},
    "load_current_a": {"load_current_a", "il", "load_current"},
    "temperature_c": {"temperature_c", "battery_temperature_c", "mppt_temperature_c", "temperature", "temp_c"},
    "peer_count": {"peer_count", "peers_count", "neighbor_count", "neighbour_count", "reachable_peers"},
    "transferred_files": {"transferred_files", "files_transferred", "transferred", "files_uploaded"},
    "transferred_bytes": {"transferred_bytes", "bytes_transferred", "uploaded_bytes", "bytes_uploaded"},
    "remaining_files": {"remaining_files", "files_remaining"},
    "charger_error": {"charger_error", "charger_error_code", "mppt_error"},
    "network_rssi_dbm": {"rssi_dbm", "signal_dbm", "signal_strength_dbm"},
    "latency_ms": {"latency_ms", "ping_ms", "rtt_ms"},
    "packet_loss_pct": {"packet_loss_pct", "loss_pct"},
    "witty_input_voltage_v": {"witty_input_voltage_v"},
    "witty_output_current_a": {"witty_output_current_a"},
}
NUMBER_FIELDS = set(ALIASES)
LABEL_KEYS = {"node", "hostname", "host", "node_name", "device", "device_name", "node_id"}
TIMESTAMP_KEYS = ("timestamp", "timestamp_utc", "time_utc", "datetime", "date_time", "observed_at", "observed_utc", "sampled_at", "collected_at", "created_at", "time")
EVENT_KEYS = ("event", "event_type", "event_name", "action")
LEVEL_KEYS = ("level", "severity", "log_level", "levelname")
MESSAGE_KEYS = ("message", "msg", "detail", "description")


def parse_timestamp(value: Any) -> pd.Timestamp | None:
    """Return UTC time; naive timestamps are assumed to be UTC (not proven)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Seconds or milliseconds since the Unix epoch, not relative uptime.
        if abs(value) < 1_000_000_000:
            return None
        try:
            return pd.to_datetime(value, unit="ms" if abs(value) > 100_000_000_000 else "s", utc=True)
        except (ValueError, OverflowError):
            return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 100:
        return None
    try:
        stamp = pd.to_datetime(value.replace(",", "."), utc=True, errors="coerce")
        if pd.isna(stamp) or not (2020 <= stamp.year <= 2100):
            return None
        return stamp
    except (ValueError, TypeError):
        return None


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def _node_from_path(path: str) -> str:
    match = NODE_RE.search(path)
    if match:
        return match.group(1)
    identity = NODE_ID_RE.search(path)
    return identity.group(1).lower() if identity else "Unknown"


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if -1e15 < float(value) < 1e15 else None
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)(?:\s*(?:V|W|A|C|°C|dBm|bytes|files))?\s*", value, re.I)
        return float(match.group(1)) if match else None
    return None


def _flatten_values(payload: dict, depth: int = 0):
    """Walk limited nested dicts, preserving actual measurement key names."""
    if depth > 5:
        return
    for key, value in payload.items():
        if isinstance(value, dict):
            yield from _flatten_values(value, depth + 1)
        elif isinstance(value, (str, int, float)):
            yield _normalize_key(str(key)), value


def _measurements(payload: dict) -> dict:
    flattened = dict(_flatten_values(payload))
    result = {}
    for canonical, aliases in ALIASES.items():
        for key in aliases:
            if key in flattened:
                numeric = _to_number(flattened[key])
                if numeric is not None:
                    result[canonical] = numeric
                    break
    return result


def _message_metrics(message: str) -> dict:
    metrics = {}
    for match in KV_RE.finditer(message):
        metrics[_normalize_key(match.group(1))] = match.group(2).strip("\"'")
    # Read simple textual key/value readouts without general number guessing.
    for key, pattern in (
        ("battery_voltage_v", r"\bbattery\s+(?:voltage|v)\s*[:=]\s*(-?\d+(?:\.\d+)?)"),
        ("solar_power_w", r"\b(?:solar\s+(?:power|generation)|generation)\s*[:=]\s*(-?\d+(?:\.\d+)?)"),
        ("pv_voltage_v", r"\b(?:solar|pv|panel)\s+voltage\s*[:=]\s*(-?\d+(?:\.\d+)?)"),
    ):
        match = re.search(pattern, message, re.I)
        if match:
            metrics[key] = match.group(1)
    return _measurements(metrics)


def _base(source: str, node: str, time: pd.Timestamp | None, timestamp_source: str, line: int) -> dict:
    return {"time": time, "timestamp_source": timestamp_source, "node": node,
            "source": source, "line": line, "level": "INFO", "event": "LOG_MESSAGE", "message": ""}


def _event_from_text(text: str) -> str:
    event = EVENT_RE.search(text)
    if event:
        return event.group(1)
    for category, pattern in (
        ("SHUTDOWN", r"\b(?:scheduled shutdown|shutting down|poweroff)\b"),
        ("BOOT", r"\b(?:boot completed|startup reason|booting)\b"),
        ("CONNECTIVITY_ERROR", r"\b(?:connection timed out|network unreachable|no route to host)\b"),
    ):
        if re.search(pattern, text, re.I):
            return category
    return "LOG_MESSAGE"


def _line_record(text: str, source: str, line_no: int) -> dict:
    detected = DATE_RE.search(text)
    dt = parse_timestamp(detected.group("date")) if detected else None
    node_match = NODE_RE.search(text)
    node = node_match.group(1) if node_match else _node_from_path(source)
    rec = _base(source, node, dt, "log" if dt is not None else "missing", line_no)
    level = LEVEL_RE.search(text)
    if level:
        rec["level"] = "WARNING" if level.group(1).upper() == "WARN" else level.group(1).upper()
    rec["event"] = _event_from_text(text)
    rec["message"] = text[:3000]
    rec.update(_message_metrics(text))
    rec.update(metrics_from_text(text))
    return rec


def _structured_record(payload: dict, source: str, index: int, modified_at: datetime | None) -> dict:
    top = {_normalize_key(str(k)): v for k, v in payload.items()}
    stamp = None
    for key in TIMESTAMP_KEYS:
        if key in top:
            stamp = parse_timestamp(top[key])
            if stamp is not None:
                break
    if stamp is None:
        # Allow timestamp nested in an explicit telemetry envelope.
        for container in ("meta", "metadata", "telemetry", "status", "sample"):
            nested = top.get(container)
            if isinstance(nested, dict):
                for key in TIMESTAMP_KEYS:
                    if key in nested:
                        stamp = parse_timestamp(nested[key])
                        if stamp is not None:
                            break
            if stamp is not None:
                break
    source_kind = "payload"
    if stamp is None and modified_at is not None:
        stamp = parse_timestamp(modified_at.isoformat())
        source_kind = "object_modified_fallback"
    if stamp is None:
        source_kind = "missing"
    node = next((str(top[k]) for k in ("node_id", "hostname", "node_name", "node", "device_name", "host") if top.get(k)), None)
    if not node:
        for nested_key in ("metadata", "meta", "identity", "autonet", "device", "agent"):
            part = top.get(nested_key)
            if isinstance(part, dict):
                node = next((str(part[k]) for k in ("node_id", "hostname", "node_name", "node") if part.get(k)), None)
                if node:
                    break
    node = node or _node_from_path(source)
    rec = _base(source, node, stamp, source_kind, index)
    event = next((str(top[k]) for k in EVENT_KEYS if top.get(k)), "STATUS_SNAPSHOT")
    rec["event"] = event[:100].upper().replace(" ", "_")
    if isinstance(top.get("event"), dict):
        rec["event"] = "STATUS_SNAPSHOT"
    rec["level"] = next((str(top[k]).upper() for k in LEVEL_KEYS if top.get(k)), "INFO")
    if rec["level"] == "WARN":
        rec["level"] = "WARNING"
    rec["message"] = next((str(top[k])[:3000] for k in MESSAGE_KEYS if isinstance(top.get(k), str)), "")
    rec.update(_measurements(payload))
    # Context-aware aliases, nested AutoNet peer lists, and raw Victron VE.Direct.
    rec.update(metrics_from_payload(payload))
    # Explicit charger_state is a code defined by the Victron VE.Direct parser.
    energy = (payload.get('data') or {}).get('energy') if isinstance(payload.get('data'), dict) else None
    battery = energy.get('battery') if isinstance(energy, dict) else None
    if isinstance(battery, dict):
        for field in ('charger_state', 'charger_error'):
            val = _to_number(battery.get(field))
            if val is not None:
                rec[field] = val
    rec.update(topology_from_payload(payload))
    rec.update(extract_cycle(payload))
    # Project-Limelight B2 'weekly' archives are bitset coverage summaries, not events.
    if top.get('kind') == 'weekly':
        details = top.get('data') if isinstance(top.get('data'), dict) else {}
        rec['is_weekly'] = True
        rec['archive_week'] = str(top.get('week', ''))
        for k in ('count', 'warnings', 'errors', 'battery_v_min', 'battery_v_max',
                  'cpu_temp_c_min', 'cpu_temp_c_max'):
            val = _to_number(details.get(k))
            if val is not None:
                rec['archive_' + k] = val
        rec['event'] = 'WEEKLY_ARCHIVE'
        rec['message'] = f"Weekly archived hourly coverage: {details.get('count', 'unknown')} slots"
    elif rec.get('is_cycle'):
        rec['event'] = 'HOURLY_CYCLE'
        if rec.get('cycle_errors'):
            rec['message'] = 'Collector flags: ' + rec['cycle_errors']
        else:
            rec['message'] = 'Hourly fleet status (no collector error codes)'
    if rec["event"] == "STATUS_SNAPSHOT" and rec["message"]:
        rec["event"] = _event_from_text(rec["message"])
    if rec["event"] == "STATUS_SNAPSHOT" and not rec["message"]:
        rec["message"] = "Structured status/telemetry snapshot"
    return rec


def parse_content(raw: bytes, source: str, modified_at: datetime | None = None, depth: int = 0) -> tuple[list[dict], list[str]]:
    """Parse bytes without extraction or execution. Returns events and warnings."""
    warnings = []
    lower = source.lower()
    size_limit = 200 * 1024 * 1024 if lower.endswith(".zip") else MAX_FILE_BYTES
    if len(raw) > size_limit:
        return [], [f"Skipped oversized file: {source} ({len(raw):,} bytes)"]
    if lower.endswith(".zip"):
        if depth >= MAX_DEPTH:
            return [], [f"Nested ZIP depth limit reached: {source}"]
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = [i for i in archive.infolist() if not i.is_dir()]
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    return [], [f"ZIP has too many members: {source}"]
                rows = []
                total_uncompressed = 0
                for member in members:
                    if not member.filename.lower().endswith(ALLOWED_EXTENSIONS):
                        continue
                    if member.file_size > MAX_FILE_BYTES:
                        warnings.append(f"Skipped large ZIP member: {member.filename}")
                        continue
                    total_uncompressed += member.file_size
                    if total_uncompressed > 200 * 1024 * 1024:
                        warnings.append(f"Stopped processing ZIP at 200 MB uncompressed: {source}")
                        break
                    try:
                        sub, errs = parse_content(archive.read(member), f"{source}!/{member.filename}", modified_at, depth + 1)
                        rows.extend(sub)
                        warnings.extend(errs)
                    except (RuntimeError, OSError, zipfile.BadZipFile) as exc:
                        warnings.append(f"Cannot read ZIP member {member.filename}: {exc}")
                return rows, warnings
        except (zipfile.BadZipFile, OSError) as exc:
            return [], [f"ZIP read error in {source}: {exc}"]
    if lower.endswith(".gz"):
        try:
            # Cap decompressed data, preventing gzip bombs.
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                unpacked = gz.read(MAX_FILE_BYTES + 1)
            if len(unpacked) > MAX_FILE_BYTES:
                return [], [f"Oversized gzip contents: {source}"]
            rows, inner_warnings = parse_content(unpacked, source[:-3], modified_at, depth)
            for row in rows:
                row['source'] = source  # Preserve the real compressed B2 object key.
            return rows, inner_warnings
        except OSError as exc:
            return [], [f"Gzip read error in {source}: {exc}"]
    text = raw.decode("utf-8-sig", errors="replace")
    rows = []
    if lower.endswith(".csv"):
        for i, record in enumerate(csv.DictReader(io.StringIO(text)), 2):
            if i > MAX_ROWS_PER_FILE:
                warnings.append(f"Row limit reached: {source}")
                break
            if record and any(record.values()):
                rows.append(_structured_record(record, source, i, modified_at))
        return rows, warnings
    if lower.endswith(".json"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                # Common event-container shapes; otherwise one status snapshot.
                seq = next((obj[k] for k in ("events", "records", "samples", "logs") if isinstance(obj.get(k), list)), None)
                if seq is None:
                    values = [obj]
                else:
                    # Preserve the parent telemetry snapshot rather than dropping
                    # it merely because the envelope includes an events array.
                    outer = {k: v for k, v in obj.items() if k not in ('events', 'records', 'samples', 'logs')}
                    has_parent_telemetry = bool(metrics_from_payload(outer)) or bool(topology_from_payload(outer))
                    values = ([outer] if has_parent_telemetry else [])
                    parent_metadata = {k: obj[k] for k in ('timestamp', 'time', 'node_id', 'hostname', 'node', 'level') if k in obj}
                    for item in seq:
                        if isinstance(item, dict):
                            values.append({**parent_metadata, **item})
            elif isinstance(obj, list):
                values = obj
            else:
                values = []
            for i, payload in enumerate(values[:MAX_ROWS_PER_FILE], 1):
                if isinstance(payload, dict):
                    rows.append(_structured_record(payload, source, i, modified_at))
            if len(values) > MAX_ROWS_PER_FILE:
                warnings.append(f"Row limit reached: {source}")
            return rows, warnings
        except json.JSONDecodeError:
            # Some objects use .json as an extension but actually contain JSON Lines.
            pass
    for i, line in enumerate(io.StringIO(text), 1):
        if i > MAX_ROWS_PER_FILE:
            warnings.append(f"Row limit reached: {source}")
            break
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(_structured_record(obj, source, i, modified_at))
                    continue
            except json.JSONDecodeError:
                pass
        rows.append(_line_record(line, source, i))
    return rows, warnings


def to_dataframe(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=["time", "timestamp_source", "node", "source", "line", "level", "event", "message", "peer_ids", *ALIASES])
    df = pd.DataFrame.from_records(records)
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    df["level"] = df["level"].fillna("INFO").astype(str)
    df["node"] = df["node"].fillna("Unknown").astype(str)
    df["event"] = df["event"].fillna("LOG_MESSAGE").astype(str)
    if "peer_ids" not in df:
        df["peer_ids"] = ""
    df["peer_ids"] = df["peer_ids"].fillna("").astype(str)
    for field in NUMBER_FIELDS | {"cpu_temperature_c"}:
        if field in df:
            df[field] = pd.to_numeric(df[field], errors="coerce")
    return df.sort_values("time", kind="stable", na_position="last").reset_index(drop=True)
