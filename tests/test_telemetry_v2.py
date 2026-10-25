import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from limelight_parser import parse_content, to_dataframe
from schema_tools import analyze_format


def test_mppt_raw_units_and_peer_graph():
    obj = {'timestamp': '2026-10-25T11:05:00Z', 'node_id': '32af66b77e9c',
           'status': {'mppt': {'V': 26430, 'VPV': 45200, 'PPV': 37, 'I': 450, 'IL': 190, 'ERR': 0},
                      'autonet': {'babel_running': True, 'peers': [{'node_id': '32dbd2292662'}]}}}
    rows, warnings = parse_content(json.dumps(obj).encode(), 'logs/32af66b77e9c/status.json')
    assert not warnings
    row = rows[0]
    assert row['node'] == '32af66b77e9c'
    assert row['battery_voltage_v'] == 26.43
    assert row['pv_voltage_v'] == 45.2
    assert row['solar_power_w'] == 37
    assert row['charge_current_a'] == .45
    assert row['charger_error'] == 0
    assert row['peer_count'] == 1
    assert row['peer_ids'] == '32dbd2292662'


def test_schema_report_does_not_include_values():
    obj = {'node_id': '32dbd2292662', 'battery_voltage_v': 26.45, 'secret': 'DONT_EXPORT_VALUE'}
    schema = analyze_format(json.dumps(obj).encode(), 'a.json')
    assert any(x['field_path'] == 'battery_voltage_v' for x in schema)
    assert 'DONT_EXPORT_VALUE' not in json.dumps(schema)
    assert '32dbd2292662' not in json.dumps(schema)


def test_parent_snapshot_and_child_events():
    payload = {'node_id': 'Limenode2', 'timestamp': '2026-10-25T12:00:00Z',
               'telemetry': {'battery_voltage_v': 26.4},
               'events': [{'event': 'WINDOW_OPEN'}, {'event': 'WINDOW_CLOSED'}]}
    rows, warnings = parse_content(json.dumps(payload).encode(), 'status.json')
    assert len(rows) == 3 and not warnings
    assert rows[0]['battery_voltage_v'] == 26.4
    assert [r['event'] for r in rows[1:]] == ['WINDOW_OPEN', 'WINDOW_CLOSED']
    assert all(r['node'] == 'Limenode2' for r in rows)


def test_missing_measurements_not_zero():
    obj = {'timestamp':'2026-10-25T12:00:00Z','level':'WARNING','message':'watchdog warning','node_id':'32af66b77e9c'}
    rows,_ = parse_content(json.dumps(obj).encode(), 'logs/32af66b77e9c/alert.json')
    df = to_dataframe(rows)
    assert 'battery_voltage_v' not in df or df['battery_voltage_v'].isna().all()
    assert 'solar_power_w' not in df or df['solar_power_w'].isna().all()
