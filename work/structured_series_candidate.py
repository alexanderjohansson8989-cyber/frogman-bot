"""Offline, bounded monthly JSON projection. Not wired into a live workflow."""
import hashlib
import json
import re


def _month(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', value):
        raise ValueError('Invalid monthly label')
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def project_months(raw, *, time_field, fields, start, end, max_rows=120, max_chars=60000):
    """Select explicit fields and inclusive months, retaining source row pointers.

    Values (including sentinels) are unchanged. Publication vintage, units and
    target relevance must be established separately; date filtering is NOT a
    historical-information cutoff. Reject ambiguity instead of clipping rows.
    """
    if not isinstance(raw, bytes) or len(raw) > 1000000:
        raise ValueError('Invalid or oversized source bytes')
    _month(start)
    _month(end)
    if start > end:
        raise ValueError('Reversed interval')
    if (not isinstance(time_field, str) or not isinstance(fields, list)
            or not fields or len(fields) > 12
            or any(not isinstance(f, str) or not f for f in fields)
            or len(set(fields)) != len(fields) or time_field in fields):
        raise ValueError('Invalid selected fields')
    if (type(max_rows) is not int or not 1 <= max_rows <= 120
            or type(max_chars) is not int or not 1 <= max_chars <= 60000):
        raise ValueError('Invalid projection limits')
    def reject_constant(value):
        raise ValueError('Non-finite JSON number')
    rows = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs,
                      parse_constant=reject_constant)
    if not isinstance(rows, list) or not rows or len(rows) > 20000:
        raise ValueError('Expected bounded nonempty row array')
    selected, labels = [], set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError('Expected row object')
        label = _month(row.get(time_field))
        if label in labels:
            raise ValueError('Duplicate month')
        labels.add(label)
        if start <= label <= end:
            if any(field not in row for field in fields):
                raise ValueError('Selected field missing')
            if any(isinstance(row[field], (list, dict)) for field in fields):
                raise ValueError('Selected field must be scalar')
            selected.append({'source_pointer': '/' + str(index),
                             'values': {key: row[key] for key in [time_field] + fields}})
    if not selected or len(selected) > max_rows:
        raise ValueError('Empty or oversized selection')
    selected.sort(key=lambda entry: entry['values'][time_field])
    expected = []
    year, month = map(int, start.split('-'))
    while f'{year:04d}-{month:02d}' <= end:
        if len(expected) >= 120:
            raise ValueError('Requested interval exceeds 120 months')
        expected.append(f'{year:04d}-{month:02d}')
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    result = {'version': 'monthly-projection-1',
              'source_sha256': hashlib.sha256(raw).hexdigest(),
              'source_bytes': len(raw), 'source_rows': len(rows),
              'selection': {'time_field': time_field, 'fields': fields,
                            'start_inclusive': start, 'end_inclusive': end},
              'selected_rows': len(selected), 'omitted_rows': len(rows) - len(selected),
              'missing_months': [label for label in expected if label not in labels],
              'vintage_verified': False, 'units_verified': False,
              'sentinels_interpreted': False, 'rows': selected}
    serialized = json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if len(serialized) > max_chars:
        raise ValueError('Projected text exceeds limit')
    return result
