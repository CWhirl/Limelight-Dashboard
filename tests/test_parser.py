import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from limelight_parser import parse_content, to_dataframe, parse_timestamp


def test_logging_events_and_metrics():
    text = b"2026-10-25 12:00:00,001 [INFO] WINDOW_OPEN\n2026-10-25 12:00:01 [INFO] SESSION_BATCH number=1 transferred=3 remaining=0\n2026-10-25 12:00:02 [WARN] something odd\n"
    rows, warnings = parse_content(text, "Limenode4/limelight.log")
    assert len(rows) == 3 and not warnings
    assert rows[0]["event"] == "WINDOW_OPEN"
    assert rows[1]["transferred_files"] == 3
    assert rows[2]["level"] == "WARNING"
    assert rows[0]["node"] == "Limenode4"
    assert rows[0]["time"].hour == 12


def test_nested_json_status_and_timestamp_fallback():
    obj = {"hostname": "Limenode1", "telemetry": {"battery_voltage_v": 26.43, "solar_power_w": 18, "charger_error": 0}}
    time = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    rows, warnings = parse_content(json.dumps(obj).encode(), "Limenode1/status.json", time)
    assert rows[0]["timestamp_source"] == "object_modified_fallback"
    assert rows[0]["battery_voltage_v"] == 26.43
    assert rows[0]["solar_power_w"] == 18
    assert rows[0]["time"].hour == 12


def test_jsonlines_and_zip():
    content = b'{"timestamp":"2026-10-25T12:00:00Z","node":"Limenode2","event":"PEER_FOUND","peer_count":2}\n'
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("Limenode2/net.jsonl", content)
        z.writestr("recording.mp4", b"binary ignored")
    rows, warnings = parse_content(bio.getvalue(), "Limelight.zip")
    assert not warnings and len(rows) == 1
    assert rows[0]["peer_count"] == 2
    assert to_dataframe(rows)["time"].notna().all()


def test_missing_time_is_not_fabricated():
    rows, warnings = parse_content(b"GPIO26_ON\n", "Limenode1/controller.log")
    assert rows[0]["time"] is None
    assert rows[0]["timestamp_source"] == "missing"


def test_bad_zip_is_safe():
    rows, warnings = parse_content(b"not an archive", "broken.zip")
    assert not rows and warnings
