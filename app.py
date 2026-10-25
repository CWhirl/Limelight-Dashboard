"""Project Limelight fleet telemetry dashboard (read-only)."""

from __future__ import annotations

import os
import json
import math
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from b2_source import connect, list_log_objects, read_object
from limelight_parser import MAX_FILE_BYTES, parse_content, to_dataframe
from schema_tools import analyze_format

st.set_page_config(page_title="Limelight | Fleet Observatory", page_icon="🌿", layout="wide")

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; max-width: 1550px; }
    div[data-testid="stMetric"] { background: rgba(120, 170, 140, .08); padding: 12px; border-radius: 10px; }
    .subtle-note { color: #7d8d82; font-size: .90rem; }
</style>
""", unsafe_allow_html=True)


def _reset_dataset(records, warnings, manifest, source_label, is_demo=False, schema=None):
    st.session_state["dataset"] = to_dataframe(records)
    st.session_state["parse_warnings"] = warnings
    st.session_state["manifest"] = manifest
    st.session_state["source_label"] = source_label
    st.session_state["is_demo"] = is_demo
    st.session_state["field_schema"] = schema or []


def _load_file(raw: bytes, name: str, modified: datetime | None = None):
    rows, warnings = parse_content(raw, name, modified)
    return rows, warnings, analyze_format(raw, name)


def _from_b2(client, bucket: str, prefix: str, max_objects: int, max_mb: int, scan_limit: int):
    manifest, truncated = list_log_objects(client, bucket, prefix, scan_limit)
    warnings = []
    if truncated:
        warnings.append(f"The listing was capped at {scan_limit:,} keys. Narrow the prefix if log files are missing.")
    total_size = 0
    loaded = []
    records = []
    schema_rows = []
    all_candidates = len(manifest)
    progress = st.progress(0, text=f"Found {all_candidates:,} candidate log objects")
    max_bytes = max_mb * 1024 * 1024
    for idx, entry in enumerate(manifest[:max_objects]):
        name = entry["key"]
        size = entry["size"]
        if size > MAX_FILE_BYTES:
            warnings.append(f"Skipped >20 MB object: {name}")
            continue
        if total_size + size > max_bytes:
            warnings.append("Reached download budget; use a more specific B2 prefix or increase the budget.")
            break
        try:
            raw = read_object(client, bucket, name, MAX_FILE_BYTES)
            if len(raw) > MAX_FILE_BYTES:
                warnings.append(f"Skipped oversized object: {name}")
                continue
            more_rows, errs, found_schema = _load_file(raw, name, entry["modified"])
            schema_rows.extend(found_schema)
            total_size += len(raw)
            records.extend(more_rows)
            warnings.extend(errs)
            loaded.append({"object": name, "size_kb": round(len(raw) / 1024, 2),
                           "modified_utc": entry["modified"].isoformat(),
                           "parsed_rows": len(more_rows)})
        except Exception as exc:
            warnings.append(f"Cannot read {name}: {type(exc).__name__}: {exc}")
        finally:
            progress.progress(min(1.0, (idx + 1) / max(1, min(all_candidates, max_objects))),
                              text=f"Reading B2 logs: {idx + 1}/{min(all_candidates, max_objects)}")
    progress.empty()
    if all_candidates > max_objects:
        warnings.append(f"Only newest {max_objects:,} of {all_candidates:,} matching objects were selected.")
    return records, warnings, loaded, schema_rows


def _chart_line(df: pd.DataFrame, field: str, ylabel: str):
    if field not in df.columns:
        st.info(f"No {ylabel.lower()} field found in the loaded logs.")
        return
    subset = df[df["time"].notna() & df[field].notna()]
    if subset.empty:
        st.info(f"No timestamped {ylabel.lower()} measurements found.")
        return
    fig = px.line(subset, x="time", y=field, color="node_label", markers=len(subset) < 300,
                  hover_data=["source", "timestamp_source"], labels={"time": "UTC", field: ylabel, "node_label": "Node"})
    fig.update_layout(height=365, margin=dict(l=0, r=0, t=12, b=0), legend_title_text="Node")
    st.plotly_chart(fig, use_container_width=True)


def _message_table(df: pd.DataFrame, max_rows: int = 1200):
    cols = [c for c in ["time", "node_label", "node", "level", "event", "message", "source", "line", "timestamp_source"] if c in df]
    st.dataframe(df[cols].sort_values("time", ascending=False, na_position="last").head(max_rows),
                 use_container_width=True, hide_index=True)




# These IDs/labels are copied only from the source repo's trusted roster.
# No public keys, credentials, or other identity secrets are packaged.
def _trusted_labels():
    path = Path(__file__).with_name('fleet_node_labels.json')
    try:
        return json.loads(path.read_text(encoding='utf-8')).get('labels', {})
    except (OSError, ValueError):
        return {}


KNOWN_NODES = _trusted_labels()


def _display_node(node: str) -> str:
    node = str(node)
    alias = st.session_state.get('node_aliases', {}).get(node.lower())
    return alias if alias else node


def _node_alias_map(df: pd.DataFrame) -> dict:
    aliases = dict(KNOWN_NODES)
    if 'hostname' in df.columns:
        for node, subset in df.groupby('node'):
            if node.lower() in aliases:
                continue  # Registered identity takes precedence over mutable hostname.
            seen = subset.dropna(subset=['hostname']).sort_values('time', na_position='first')
            names = seen['hostname'].astype(str).str.strip()
            names = names[(names != '') & (names.str.lower() != 'none')]
            if not names.empty:
                aliases[str(node).lower()] = names.iloc[-1]
    return aliases


def _add_node_labels(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['node_label'] = df['node'].map(_display_node)
    return df


def _observed_identity_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for node, group in df.groupby('node'):
        current = group.sort_values('time', na_position='first').iloc[-1]
        hostname = current.get('hostname')
        trusted = KNOWN_NODES.get(str(node).lower())
        hostname = hostname if isinstance(hostname, str) and hostname.strip() else None
        rows.append({
            'Node': trusted or hostname or node,
            'Registered node ID': node,
            'Trusted roster name': trusted or '(not enrolled in bundled roster)',
            'Recorded hostname': hostname or '(not provided)',
            'Name agrees': 'YES' if (trusted and hostname and trusted.lower() == hostname.lower())
                         else 'CHECK' if (trusted and hostname and trusted.lower() != hostname.lower()) else '—',
            'Last sample (UTC)': current['time'],
        })
    return pd.DataFrame(rows)


def _metric_latest(df: pd.DataFrame, fields: list[str]) -> pd.DataFrame:
    # A missing observation must remain blank: don't carry values forward indefinitely.
    results = []
    for node, grp in df.groupby('node'):
        row = {'Node': _display_node(node)}
        for field in fields:
            if field in grp.columns:
                observed = grp[grp[field].notna() & grp['time'].notna()].sort_values('time')
                row[field] = observed.iloc[-1][field] if not observed.empty else None
                row[field + '_time'] = observed.iloc[-1]['time'] if not observed.empty else pd.NaT
        results.append(row)
    return pd.DataFrame(results)


def _show_latest(df, fields, title):
    existing = [f for f in fields if f in df and df[f].notna().any()]
    if not existing:
        st.info('No measurements for these fields were found. Check Data quality → Field discovery to see what B2 actually contains.')
        return
    st.subheader(title)
    st.dataframe(_metric_latest(df, existing), hide_index=True, use_container_width=True)
    st.caption('Each metric includes its own last-observed UTC timestamp. Old readings are not presented as current live measurements.')


def _network_edges(df):
    if 'peer_ids' not in df:
        return []
    latest = df[df['peer_ids'].fillna('').astype(str).str.strip().str.len().gt(0)].sort_values('time').groupby('node').tail(1)
    edges = []
    for _, row in latest.iterrows():
        for peer in str(row['peer_ids']).split('|'):
            peer = peer.strip()
            if peer and peer != row['node']:
                edges.append((str(row['node']), peer))
    return list(dict.fromkeys(edges))


def _topology_chart(edges):
    if not edges:
        st.info('No nonempty AutoNet peer advertisements found. Empty lists do not establish a link failure; the fleet logger already supports saving the peer list when present.')
        return
    nodes = sorted(set(n for e in edges for n in e))
    coords = {n: (math.cos(i*2*math.pi/len(nodes)), math.sin(i*2*math.pi/len(nodes))) for i,n in enumerate(nodes)}
    traces = []
    for start, end in edges:
        traces.append(go.Scatter(x=[coords[start][0], coords[end][0]], y=[coords[start][1], coords[end][1]],
                                 mode='lines', line=dict(width=2, color='rgba(80,180,150,.48)'),
                                 hoverinfo='text', text=[f'{start} reported {end}', f'{start} reported {end}'], showlegend=False))
    traces.append(go.Scatter(x=[coords[n][0] for n in nodes], y=[coords[n][1] for n in nodes], mode='markers+text',
                             marker=dict(size=25, color='#67CBA6'), text=[_display_node(n) for n in nodes],
                             textposition='top center', hovertext=nodes, hoverinfo='text', showlegend=False))
    fig = go.Figure(traces)
    fig.update_layout(height=450, margin=dict(t=40, b=10, l=10, r=10), xaxis=dict(visible=False), yaxis=dict(visible=False),
                      plot_bgcolor='rgba(0,0,0,0)')
    st.plotly_chart(fig, use_container_width=True)
    st.caption('Observed peer advertisements in the latest available record for each node; links are not independently verified or necessarily simultaneous.')


st.title("🌿 Limelight Fleet Observatory")
st.caption("Interactive diagnostics from Backblaze B2, exported ZIPs, or included synthetic demonstration logs. All times are UTC.")

with st.sidebar:
    st.header("Data source")
    source_mode = st.radio("Select source", ["Backblaze B2", "Local ZIP", "Demo data"], index=0)

    if source_mode == "Backblaze B2":
        st.caption("B2 is object storage, not a queryable database. This app lists and reads log objects using read-only API calls.")
        with st.form("connection_form"):
            endpoint = st.text_input("S3 endpoint", value=os.getenv("B2_S3_ENDPOINT", ""),
                                     placeholder="https://s3.us-west-004.backblazeb2.com")
            bucket = st.text_input("Bucket name", value=os.getenv("B2_BUCKET", ""), placeholder="my-limelight-logs")
            prefix = st.text_input("Object prefix (optional)", value=os.getenv("B2_PREFIX", ""), placeholder="logs/")
            key_id = st.text_input("Application key ID", value="", placeholder="Or set B2_KEY_ID")
            application_key = st.text_input("Application key", value="", type="password",
                                            placeholder="Or set B2_APPLICATION_KEY")
            connect_pressed = st.form_submit_button("Connect to B2", use_container_width=True)
        if connect_pressed:
            try:
                key_id = key_id or os.getenv("B2_KEY_ID", "")
                application_key = application_key or os.getenv("B2_APPLICATION_KEY", "")
                if not all([endpoint, bucket, key_id, application_key]):
                    raise ValueError("Provide endpoint, bucket and a read-only B2 key ID/application key pair.")
                client = connect(endpoint.rstrip("/"), key_id, application_key)
                # Scoped application keys may not support ListBuckets or HeadBucket.
                client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
                st.session_state["b2_client"] = client
                st.session_state["b2_bucket"] = bucket
                st.session_state["b2_prefix"] = prefix
                st.session_state["b2_endpoint"] = endpoint
                st.success("Connected. Ready to read log objects.")
            except Exception as exc:
                st.error(f"Connection failed: {type(exc).__name__}: {exc}")
        if "b2_client" in st.session_state:
            st.success(f"Connected: {st.session_state['b2_bucket']}")
            if st.button("Disconnect"):
                for key in ["b2_client", "b2_bucket", "b2_prefix", "b2_endpoint"]:
                    st.session_state.pop(key, None)
                st.rerun()
            max_objects = st.number_input("Maximum objects per refresh", min_value=1, max_value=1000, value=150, step=25)
            max_mb = st.number_input("Download budget (MB)", min_value=1, max_value=500, value=50)
            scan_limit = st.number_input("Maximum keys to scan", min_value=100, max_value=100000, value=10000, step=1000)
            if st.button("Fetch / refresh B2 logs", type="primary", use_container_width=True):
                try:
                    rows, warnings, objects, schema = _from_b2(
                        st.session_state["b2_client"], st.session_state["b2_bucket"],
                        st.session_state["b2_prefix"], int(max_objects), int(max_mb), int(scan_limit)
                    )
                    _reset_dataset(rows, warnings, objects,
                                   f"B2: {st.session_state['b2_bucket']}/{st.session_state['b2_prefix']}", schema=schema)
                    st.success(f"Loaded {len(rows):,} parsed records from {len(objects):,} objects")
                except Exception as exc:
                    st.error(f"Log retrieval failed: {type(exc).__name__}: {exc}")
    elif source_mode == "Local ZIP":
        uploaded = st.file_uploader("Upload Limelight master ZIP", type=["zip"])
        if uploaded and st.button("Parse ZIP", type="primary", use_container_width=True):
            rows, warnings, schema = _load_file(uploaded.getvalue(), uploaded.name)
            _reset_dataset(rows, warnings, [{"object": uploaded.name, "size_kb": round(uploaded.size / 1024),
                                              "parsed_rows": len(rows)}], f"ZIP: {uploaded.name}", schema=schema)
            st.success(f"Parsed {len(rows):,} records")
    else:
        st.info("Demonstration measurements are simulated—not actual Limelight field data.")
        if st.button("Load synthetic demo", type="primary", use_container_width=True):
            archive = Path(__file__).with_name("limelight_demo.zip")
            rows, warnings, schema = _load_file(archive.read_bytes(), archive.name)
            _reset_dataset(rows, warnings, [{"object": archive.name, "parsed_rows": len(rows)}],
                           "SYNTHETIC DEMO", True, schema)

    st.divider()
    st.caption("Credentials remain in this Streamlit server session. Do not expose the dashboard publicly without authentication. Use a bucket-scoped, read-only B2 application key.")

if "dataset" not in st.session_state:
    st.info("Choose **Backblaze B2**, **Local ZIP**, or **Demo data** in the sidebar to start. The dashboard never writes to B2 or changes the Raspberry Pis.")
    st.stop()

full = st.session_state["dataset"]
if st.session_state.get("is_demo"):
    st.warning("SYNTHETIC DEMO DATA — these plots do not represent actual field measurements.")
st.caption(f"Dataset: **{st.session_state.get('source_label', 'Unknown')}** · {len(full):,} parsed rows · {len(st.session_state.get('manifest', [])):,} files")

if full.empty:
    st.warning("No log records were parsed. Use Data Quality to inspect files and warnings, or provide a representative log sample for custom parsing.")
    with st.expander("Import warnings / files", expanded=True):
        st.write(st.session_state.get("parse_warnings", []))
        st.dataframe(st.session_state.get("manifest", []))
    st.stop()

st.session_state['node_aliases'] = _node_alias_map(full)
full = _add_node_labels(full)

with st.expander("Node identification", expanded=False):
    st.caption("Names come from config/trust/roster.json in your uploaded Project-Limelight repository. Data.hostname is displayed for comparison but cannot override a trusted ID-to-label mapping. Physical installation/site is not inferred.")
    table = _observed_identity_table(full)
    st.dataframe(table, hide_index=True, use_container_width=True)
    if 'CHECK' in table['Name agrees'].values:
        st.warning('At least one uploaded hostname disagrees with the committed trust roster; inspect that device before using its reported name.')

with st.expander("Filters", expanded=True):
    a, b, c = st.columns([2, 2, 2])
    node_options = sorted(full["node"].dropna().unique().tolist())
    selected_node_labels = a.multiselect('Nodes', [_display_node(n) for n in node_options], default=[_display_node(n) for n in node_options])
    selected_nodes = [n for n in node_options if _display_node(n) in selected_node_labels]
    selected_levels = b.multiselect("Severity", sorted(full["level"].dropna().unique().tolist()),
                                    default=sorted(full["level"].dropna().unique().tolist()))
    dated = full.loc[full["time"].notna(), "time"]
    if not dated.empty:
        min_date, max_date = dated.min().date(), dated.max().date()
        picked = c.date_input("UTC date range", value=(min_date, max_date), min_value=min_date, max_value=max_date)
    else:
        picked = ()

filtered = full[full["node"].isin(selected_nodes) & full["level"].isin(selected_levels)].copy()
if isinstance(picked, tuple) and len(picked) == 2:
    start = pd.Timestamp(picked[0], tz="UTC")
    end = pd.Timestamp(picked[1], tz="UTC") + pd.Timedelta(days=1)
    filtered = filtered[filtered["time"].isna() | ((filtered["time"] >= start) & (filtered["time"] < end))]

if filtered.empty:
    st.info("No records match these filters.")
    st.stop()

error_mask = filtered["level"].str.contains("ERROR|CRITICAL|FATAL", case=False, na=False)
last_event = filtered["time"].max()
metrics = st.columns(5)
metrics[0].metric("Nodes represented", filtered["node"].nunique())
metrics[1].metric("Parsed records", f"{len(filtered):,}")
metrics[2].metric("Errors / critical", f"{error_mask.sum():,}")
metrics[3].metric("Warnings", f"{filtered['level'].eq('WARNING').sum():,}")
metrics[4].metric("Last event (UTC)", last_event.strftime("%m-%d %H:%M") if pd.notna(last_event) else "Unknown")

tabs = st.tabs(["Fleet overview", "Battery & solar", "Comms & transfers", "Relay timeline", "Raw events", "Data quality", "Weekly archive"])

with tabs[0]:
    st.subheader("Event activity over time")
    data = filtered[filtered["time"].notna()].copy()
    if not data.empty:
        data["hour"] = data["time"].dt.floor("h")
        hourly = data.groupby(["hour", "node_label"]).size().reset_index(name="events")
        fig = px.bar(hourly, x="hour", y="events", color="node_label", labels={"hour": "UTC hour", "events": "Log entries", "node_label": "Node"})
        fig.update_layout(height=365, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Log activity is not system uptime; silent periods may be scheduled shutdowns, missing uploads, or missing timestamps.")
    else:
        st.info("No timestamped events available for an activity chart.")
    left, right = st.columns(2)
    with left:
        st.subheader("Events by node")
        counts = filtered.groupby("node_label").size().reset_index(name="entries")
        st.plotly_chart(px.bar(counts, x="node_label", y="entries", labels={"entries": "Log entries", "node_label": "Node"}), use_container_width=True)
    with right:
        st.subheader("Error / warning counts")
        alerts = filtered[filtered["level"].isin(["WARNING", "ERROR", "CRITICAL", "FATAL"])]
        if alerts.empty:
            st.info("No warning or error entries found in the selected files (not proof of a healthy fleet).")
        else:
            by_level = alerts.groupby(["node_label", "level"]).size().reset_index(name="entries")
            st.plotly_chart(px.bar(by_level, x="node_label", y="entries", color="level", barmode="stack"), use_container_width=True)
    st.subheader("Most recent observed timestamp per node")
    latest = (filtered.dropna(subset=["time"]).groupby("node")["time"].max().reset_index(name="last_observed_utc"))
    latest['Node'] = latest['node'].map(_display_node)
    st.dataframe(latest[['Node','last_observed_utc']], hide_index=True, use_container_width=True)
    if 'is_cycle' in filtered.columns:
        st.subheader('Hourly cycle coverage (observed, not uptime)')
        cycles = filtered[(filtered['is_cycle'].eq(True)) & filtered['time'].notna()].copy()
        if not cycles.empty:
            cycles['hour'] = cycles['time'].dt.floor('h')
            presence = cycles.drop_duplicates(['node', 'hour'])
            fig = px.scatter(presence, x='hour', y='node_label', color='level',
                             labels={'hour':'UTC cycle slot', 'node_label':'Node', 'level':'Collector severity'})
            fig.update_traces(marker={'size': 11})
            st.plotly_chart(fig, use_container_width=True)
            st.caption('Dots mean this node has an uploaded observation for that UTC hour. Missing dots can mean no collection, no upload, intentional shutdown, or a gap in loaded B2 objects.')

    if 'cycle_errors' in filtered.columns and filtered['cycle_errors'].fillna('').astype(str).str.len().gt(0).any():
        st.subheader('Collector warning causes (as recorded on the Pi)')
        causes = filtered[filtered['cycle_errors'].fillna('').astype(str).str.len().gt(0)][['time','node_label','level','cycle_errors']].sort_values('time',ascending=False)
        st.dataframe(causes.head(200), hide_index=True, use_container_width=True)
        st.caption('An MPPT-unavailable warning on a bench-powered node is a telemetry-availability finding, not proof of low battery or network failure.')
    if 'health_throttle_flags' in filtered or 'health_failed_services' in filtered:
        st.subheader('Latest Pi health diagnostics')
        latest_health = _metric_latest(filtered, [c for c in ('health_throttle_flags','health_failed_services','health_recent_errors') if c in filtered])
        st.dataframe(latest_health, hide_index=True, use_container_width=True)

with tabs[1]:
    if 'cpu_temperature_c' in filtered.columns and filtered['cpu_temperature_c'].notna().any():
        st.subheader('Raspberry Pi CPU temperature (reported)')
        temp = filtered.dropna(subset=['cpu_temperature_c','time'])
        st.plotly_chart(px.line(temp, x='time', y='cpu_temperature_c', color='node_label', markers=True, labels={'cpu_temperature_c':'CPU temperature (°C)','time':'UTC','node_label':'Node'}), use_container_width=True)
    if 'thermal_mean_c' in filtered.columns and filtered['thermal_mean_c'].notna().any():
        st.subheader('Previous-hour CPU temperature summary (5-minute samples)')
        thermal = filtered.dropna(subset=['thermal_mean_c','time'])
        st.plotly_chart(px.line(thermal, x='time', y='thermal_mean_c', color='node_label', markers=True,
                                 labels={'thermal_mean_c':'Mean CPU temperature (°C)', 'time':'UTC','node_label':'Node'}), use_container_width=True)
        st.caption('The repo stores up to 12 CPU temperature readings per preceding hour when the sampling timer is active; this line shows the reported mean, not raw 5-minute readings.')
    if 'energy_telemetry_status' in filtered.columns:
        st.subheader('Energy telemetry availability by node')
        energy = filtered.dropna(subset=['energy_telemetry_status'])
        if not energy.empty:
            states = energy.groupby(['node_label','energy_telemetry_status']).size().reset_index(name='snapshots')
            st.plotly_chart(px.bar(states,x='node_label',y='snapshots',color='energy_telemetry_status',labels={'node_label':'Node','snapshots':'Cycle records','energy_telemetry_status':'Reported telemetry status'}),use_container_width=True)
    st.caption('CPU temperature does not require an MPPT. Battery/solar metrics require successfully reported VE.Direct telemetry. No missing values are set to zero.')
    _show_latest(filtered, ["battery_voltage_v", "solar_power_w", "pv_voltage_v", "charge_current_a", "load_current_a", "charger_error"], "Latest reported power readings by node")
    st.subheader("Battery bank voltage")
    _chart_line(filtered, "battery_voltage_v", "Battery voltage (V)")
    st.subheader("Solar generation")
    _chart_line(filtered, "solar_power_w", "PV power (W)")
    a, b = st.columns(2)
    with a:
        st.subheader("PV voltage")
        _chart_line(filtered, "pv_voltage_v", "PV voltage (V)")
    with b:
        st.subheader("Charge current (when logged)")
        _chart_line(filtered, "charge_current_a", "Reported charge current (A)")
    left, right = st.columns(2)
    with left:
        st.subheader('Load current (when logged)')
        _chart_line(filtered, 'load_current_a', 'Load current (A)')
    with right:
        st.subheader('MPPT error code (when logged)')
        _chart_line(filtered, 'charger_error', 'MPPT error code')
    if 'charger_state' in filtered.columns and filtered['charger_state'].notna().any():
        st.subheader('Victron charger state code (when reported)')
        _chart_line(filtered, 'charger_state', 'Charger state code')
    st.caption("Battery voltage is not state of charge. The repo records MPPT output current, not net battery current. Missing values are never interpolated or set to zero.")

with tabs[2]:
    if 'selected_gateway_id' in filtered.columns and filtered['selected_gateway_id'].notna().any():
        st.subheader('Selected gateway by reporting node')
        gw = filtered.dropna(subset=['selected_gateway_id','time']).copy()
        gw['Gateway'] = gw['selected_gateway_id'].astype(str).map(_display_node)
        gw['Reporter'] = gw['node_label']
        newest = gw.sort_values('time').groupby('node').tail(1)
        st.dataframe(newest[['Reporter','Gateway','time']].rename(columns={'time':'Observation (UTC)'}), hide_index=True, use_container_width=True)
        counts = gw.groupby(['Gateway','Reporter'],as_index=False).size()
        st.plotly_chart(px.bar(counts, x='Reporter', y='size', color='Gateway', barmode='stack', labels={'size':'Recorded gateway selections'}), use_container_width=True)
        st.caption('The collector saves the gateway choice but removes Babel state and direct uplink test flags. Selected gateway is NOT confirmed network connectivity, ping, or radio power.')
    else:
        st.info('No selected gateway records found in the loaded data.')
    if 'autonet_last_updated_unix' in filtered.columns:
        net = filtered.dropna(subset=['autonet_last_updated_unix','time']).copy()
        if not net.empty:
            net['Status age (seconds)'] = ((net['time'].astype('int64') / 1e9) - net['autonet_last_updated_unix']).round(0)
            valid = net[net['Status age (seconds)'].between(-3600, 3600)]
            if not valid.empty:
                st.subheader('AutoNet status freshness at collection')
                st.plotly_chart(px.line(valid, x='time', y='Status age (seconds)', color='node_label', markers=True,
                                         labels={'time':'UTC', 'node_label':'Node'}), use_container_width=True)
                st.caption('Age of the last AutoNet status update when the Pi captured its hourly record. Fresh status does not prove physical network connectivity.')
    if 'autonet_peer_count' in filtered.columns and filtered['autonet_peer_count'].notna().any():
        st.subheader('Reported AutoNet peer advertisements')
        _chart_line(filtered, 'autonet_peer_count', 'Discovered peers')
    _show_latest(filtered, ['peer_count', 'network_rssi_dbm', 'latency_ms', 'packet_loss_pct'], 'Latest reported connectivity metrics')
    st.subheader('Observed peer topology')
    edges = _network_edges(filtered)
    _topology_chart(edges)
    if edges:
        st.dataframe(pd.DataFrame([{'Reporter': _display_node(src), 'Reported peer': _display_node(dst)} for src, dst in edges]),
                     hide_index=True, use_container_width=True)
    a, b = st.columns(2)
    with a:
        st.subheader("Peer count (when logged)")
        _chart_line(filtered, "peer_count", "Peer count")
    with b:
        st.subheader("Received signal (when logged)")
        _chart_line(filtered, "network_rssi_dbm", "Signal (dBm)")
    a, b = st.columns(2)
    with a:
        st.subheader('Reported latency')
        _chart_line(filtered, 'latency_ms', 'Latency (ms)')
    with b:
        st.subheader('Packet loss')
        _chart_line(filtered, 'packet_loss_pct', 'Packet loss (%)')
    st.subheader("Logged transfer activity")
    transfer = filtered[(filtered.get("transferred_files", pd.Series(index=filtered.index, dtype=float)).notna()) |
                        (filtered["event"].str.contains("TRANSFER|SESSION_BATCH|SYNC_RESULT|UPLOAD", case=False, na=False))]
    if not transfer.empty:
        a, b = st.columns(2)
        with a:
            if "transferred_files" in transfer and transfer["transferred_files"].notna().any():
                transfer_counts = transfer.dropna(subset=["transferred_files"]).groupby("node", as_index=False)["transferred_files"].sum()
                st.plotly_chart(px.bar(transfer_counts, x="node", y="transferred_files", labels={"transferred_files": "Files reported in transfer events"}), use_container_width=True)
            else:
                st.info("Transfer events exist but no transferred-file counts could be extracted.")
        with b:
            if "transferred_bytes" in transfer and transfer["transferred_bytes"].notna().any():
                by_bytes = transfer.dropna(subset=["transferred_bytes"]).groupby("node", as_index=False)["transferred_bytes"].sum()
                by_bytes["mb"] = by_bytes["transferred_bytes"] / 1_000_000
                st.plotly_chart(px.bar(by_bytes, x="node", y="mb", labels={"mb": "Reported MB transferred"}), use_container_width=True)
            else:
                st.info("Transfer byte counts were not present in recognized fields.")
        st.caption("Totals sum reported event fields; repeated status snapshots or cumulative counters could double-count. Validate log semantics before treating totals as unique uploads.")
        _message_table(transfer, 400)
    else:
        st.info("No identifiable transfer events in the selected logs. This may reflect logging coverage rather than a lack of transfers.")

with tabs[3]:
    st.subheader('Hourly cycle controller state (reported)')
    states = [c for c in ['power_phase','power_action','power_armed','power_arm_pending','radio_should_be_on'] if c in filtered.columns]
    if states:
        state = filtered.dropna(subset=['time']).copy().sort_values('time')
        show = ['time','node_label'] + states
        st.dataframe(state[show].rename(columns={'time':'Observed (UTC)','node_label':'Node'}).tail(300), hide_index=True, use_container_width=True)
        if 'power_armed' in state:
            armed = state.dropna(subset=['power_armed']).copy()
            if not armed.empty:
                armed['Armed'] = armed['power_armed'].map({True:'Armed',False:'Not armed'})
                st.plotly_chart(px.scatter(armed,x='time',y='node_label',color='Armed',labels={'time':'Observed UTC','node_label':'Node'}), use_container_width=True)
    st.caption('Controller armed/phase and radio_should_be_on are reported logical states; they do not verify electrical power to radios.')
    st.subheader("Scheduled operations and relay events")
    chosen = filtered[filtered["event"].str.contains("WINDOW|RELAY|GPIO|POWERBEAM|STARTUP|SHUTDOWN|CONTROLLER|WITTY|BOOT", case=False, na=False)]
    timeline = chosen.dropna(subset=["time"]).copy()
    if not timeline.empty:
        fig = px.scatter(timeline, x="time", y="node", color="event", symbol="event",
                         hover_data=["message", "source"], labels={"time": "UTC", "node": "Node"})
        fig.update_traces(marker={"size": 11})
        fig.update_layout(height=max(320, 95 + 75 * timeline["node"].nunique()), margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No timestamped relay / schedule events recognized.")
    _message_table(chosen, 600)
    st.caption("These are log-observed events, not electrical measurements. A GPIO or relay log does not prove a PowerBeam received power.")

with tabs[4]:
    st.subheader("Browse the parsed logs")
    query = st.text_input("Search messages / events (case insensitive)", placeholder="e.g. WINDOW_OPEN, failed, GPIO26")
    events = filtered
    if query:
        hits = events["event"].str.contains(query, case=False, na=False, regex=False) | events["message"].str.contains(query, case=False, na=False, regex=False)
        events = events[hits]
    st.write(f"{len(events):,} matching entries")
    _message_table(events, 2000)
    st.download_button("Export filtered log rows as CSV", data=events.to_csv(index=False).encode("utf-8"),
                       file_name="limelight_filtered_logs.csv", mime="text/csv")

with tabs[5]:
    st.subheader("Parsing and coverage")
    st.info('If the field discovery report contains no battery/solar/peer fields, those measurements are not in the files loaded by this dashboard. The Raspberry Pi upload workflow must publish them; the dashboard cannot recover data that was never recorded.')
    st.write("Log formats supported: timestamped text, JSON Lines, JSON arrays/status snapshots, CSV, gzip and ZIP bundles. Unsupported or oversized files are skipped.")
    sources = filtered.groupby("timestamp_source", dropna=False).size().reset_index(name="rows")
    st.dataframe(sources, hide_index=True, use_container_width=True)
    fallback = int((filtered["timestamp_source"] == "object_modified_fallback").sum())
    missing = int(filtered["time"].isna().sum())
    st.info(f"{fallback:,} rows use **B2 object modification time** as a fallback (not measurement time). {missing:,} rows lack a parsable timestamp and are omitted from timeline plots.")
    metric_cols = [c for c in ["battery_voltage_v", "solar_power_w", "pv_voltage_v", "charge_current_a", "peer_count", "transferred_files", "transferred_bytes"] if c in full]
    if metric_cols:
        coverage = pd.DataFrame([{"metric": col, "rows": int(filtered[col].notna().sum())} for col in metric_cols])
        st.dataframe(coverage, hide_index=True, use_container_width=True)
    st.subheader('Field discovery (the actual schema in your B2 objects)')
    st.caption('This lists field paths and value types, not field values. Paths and object names may still be sensitive; review the downloaded report before sharing it.')
    schema = st.session_state.get('field_schema', [])
    if schema:
        fields = pd.DataFrame(schema)
        summary = (fields.groupby(['field_path', 'value_type'], as_index=False)
                        .agg(files_seen=('object', 'nunique'), observations=('observations', 'sum'))
                        .sort_values(['files_seen', 'observations'], ascending=False))
        selected = st.text_input('Find field path', placeholder='battery, solar, MPPT, peer, network, telemetry, status')
        if selected:
            summary = summary[summary['field_path'].str.contains(selected, case=False, regex=False, na=False)]
        st.dataframe(summary.head(500), hide_index=True, use_container_width=True)
        report = {'dataset': st.session_state.get('source_label'),
                  'note': 'Field paths and types only; review field names and object paths before sharing.',
                  'fields': fields.to_dict(orient='records')}
        st.download_button('Download schema report (review before sharing)',
                           json.dumps(report, indent=2).encode('utf-8'), file_name='limelight_schema_report.json',
                           mime='application/json')
    else:
        st.info('No structured field paths recognized. The logs may be plain text; a redacted sample will be needed to identify their format.')
    st.subheader('Objects read')
    st.dataframe(st.session_state.get("manifest", []), hide_index=True, use_container_width=True)
    warnings = st.session_state.get("parse_warnings", [])
    if warnings:
        st.warning(f"{len(warnings)} import warning(s)")
        st.code("\n".join(str(w) for w in warnings[:200]))
    else:
        st.success("No import warnings.")
    st.caption("Missing uploads, partial log collections, clocks with incorrect UTC, unknown naming conventions and sampling gaps limit what the dashboard can conclude.")

with tabs[6]:
    st.subheader('Historical weekly archive (28-day normal / 90-day flagged compaction)')
    if 'is_weekly' not in filtered.columns:
        st.info('No weekly archive objects loaded. The current repo exports them under limelight/v1/weekly/ once local cycle rows age out.')
    else:
        week = filtered[filtered['is_weekly'].eq(True)].copy()
        if week.empty:
            st.info('No weekly summaries in the current selection.')
        else:
            for col in ['archive_count','archive_warnings','archive_errors']:
                if col not in week:
                    week[col] = 0
            summary = week[['node_label','archive_week','archive_count','archive_warnings','archive_errors']].rename(
                columns={'node_label':'Node','archive_week':'ISO week','archive_count':'Observed hourly slots',
                         'archive_warnings':'Warning slots','archive_errors':'Error slots'})
            st.dataframe(summary.sort_values(['ISO week','Node'],ascending=[False,True]),hide_index=True,use_container_width=True)
            fig = px.bar(week, x='archive_week',y='archive_count',color='node_label', barmode='group',
                         labels={'archive_week':'ISO week','archive_count':'Recorded hourly slots','node_label':'Node'})
            st.plotly_chart(fig,use_container_width=True)
            st.caption('Archived slot coverage is not an uptime percentage; it omits unobserved hours and is not 5-minute telemetry.')
