"""Privacy-conscious field discovery: field names and types only, never raw values."""
from __future__ import annotations

import csv
import gzip
import io
import json
import re
import zipfile
from collections import Counter

from telemetry_enrichment import compact_field_paths

LIMIT = 20 * 1024 * 1024


def _record_fields(obj, counter: Counter, max_records=10):
    rows = []
    if isinstance(obj, dict):
        rows.append(obj)
        for key in ('events', 'records', 'samples', 'logs', 'readings', 'telemetry_history'):
            if isinstance(obj.get(key), list):
                rows.extend(x for x in obj[key][:max_records] if isinstance(x, dict))
    elif isinstance(obj, list):
        rows = [r for r in obj[:max_records] if isinstance(r, dict)]
    for row in rows[:max_records]:
        for key, dtype in compact_field_paths(row).items():
            counter[(key, dtype)] += 1


def analyze_format(raw: bytes, name: str, depth=0) -> list[dict]:
    """Identify field paths/types; no raw field values leave the parser."""
    lower = name.lower()
    if depth > 1 or len(raw) > LIMIT:
        return []
    if lower.endswith('.zip'):
        results = []
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for member in z.infolist()[:200]:
                    if not member.is_dir() and member.file_size <= LIMIT:
                        results.extend(analyze_format(z.read(member), f'{name}!/{member.filename}', depth+1))
        except (OSError, RuntimeError, zipfile.BadZipFile):
            pass
        return results
    if lower.endswith('.gz'):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                contents = gz.read(LIMIT + 1)
            return [{**item, 'object': name} for item in analyze_format(contents, name[:-3], depth)]
        except OSError:
            return []
    counter = Counter()
    text = raw.decode('utf-8-sig', errors='replace')
    if lower.endswith('.json'):
        try:
            _record_fields(json.loads(text), counter)
        except json.JSONDecodeError:
            pass
    elif lower.endswith('.csv'):
        rows = list(csv.DictReader(io.StringIO(text)) if len(text) < 1_000_000 else [])
        _record_fields(rows, counter)
    if not counter:
        for line in text.splitlines()[:50]:
            try:
                item = json.loads(line)
                _record_fields(item, counter)
            except (ValueError, TypeError):
                for key in re.findall(r'(?<!\w)([A-Za-z][\w.-]*)\s*[=:]', line)[:100]:
                    counter[(key, 'text-value')] += 1
    return [{'object': name, 'field_path': path, 'value_type': dtype, 'observations': count}
            for (path, dtype), count in sorted(counter.items())]
