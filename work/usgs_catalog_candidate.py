"""Fixed USGS endpoint, bounded query and strict offline event selection.

Fetch global threshold candidates, then filter geography locally. This avoids
inferring remote rectangle-edge semantics. Saturated responses are rejected;
pagination or interval subdivision is not implemented by this module.
"""
import datetime as dt
import hashlib
import json
import math
from urllib.parse import urlencode, urlsplit, parse_qsl

ENDPOINT = 'https://earthquake.usgs.gov/fdsnws/event/1/query'


class SaturatedResponse(ValueError):
    """Verified response hit the query limit; its coverage is not certified."""


def utc(value):
    if not isinstance(value, str):
        raise ValueError('Explicit UTC timestamp required')
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('Invalid timestamp') from error
    if result.tzinfo is None or result.utcoffset() != dt.timedelta(0):
        raise ValueError('Explicit UTC timestamp required')
    return result


def finite(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('Numeric parameter outside supported range')
    return float(value)


def query(spec, *, as_of, limit=500):
    if not isinstance(spec, dict) or set(spec) != {'start', 'end', 'min_magnitude', 'bounds'}:
        raise ValueError('Exact catalogue selection fields required')
    start, end, cutoff = utc(spec['start']), utc(spec['end']), utc(as_of)
    if start.microsecond % 1000 or end.microsecond % 1000:
        raise ValueError('Catalogue interval requires millisecond precision')
    if not (dt.datetime(1900,1,1,tzinfo=dt.timezone.utc) <= start < end <= cutoff
            and end - start <= dt.timedelta(days=31)):
        raise ValueError('Query must be a past interval of at most 31 days')
    magnitude = finite(spec['min_magnitude'], 4, 10)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('Unsupported result limit')
    bounds = spec['bounds']
    if bounds is not None:
        if not isinstance(bounds, dict) or set(bounds) != {'south','north','west','east'}:
            raise ValueError('Exact geographic bounds required')
        south = finite(bounds['south'], -90, 90)
        north = finite(bounds['north'], -90, 90)
        west = finite(bounds['west'], -180, 180)
        east = finite(bounds['east'], -180, 180)
        if south >= north or west == east or (west == 180 and east == -180):
            raise ValueError('Empty or ambiguous geographic box')
    params = {'format':'geojson', 'starttime':start.isoformat(), 'endtime':end.isoformat(),
              # Remote min is slightly lower; exact >= test performed below.
              'minmagnitude':format(magnitude - .000001, '.6f'),
              'eventtype':'earthquake', 'orderby':'time-asc', 'limit':str(limit),
              'includeallorigins':'false', 'includeallmagnitudes':'false',
              'includedeleted':'false'}
    return {'version':'usgs-selection-1', 'url':ENDPOINT + '?' + urlencode(params),
            'parameters':params, 'selection':json.loads(json.dumps(spec, allow_nan=False)),
            'as_of':cutoff.isoformat(), 'limit':limit,
            'time_membership':'start_inclusive_end_exclusive',
            'spatial_membership':'inclusive_local_rectangle; west>east crosses date line',
            'catalogue':'USGS current preferred origin/magnitude; historical vintage unverified'}


def _json(raw):
    def pairs(items):
        result = {}
        for key,value in items:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('Non-finite JSON constant')
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs, parse_constant=constant)


def select_events(raw, request, *, retrieved_at):
    """Archive raw separately before calling. No forecast or calibration implied."""
    if not isinstance(raw, bytes) or len(raw) > 1000000:
        raise ValueError('Invalid or oversized catalogue bytes')
    # Rebuild, so arbitrary endpoints/extra parameters cannot enter the receipt.
    rebuilt = query(request['selection'], as_of=request['as_of'], limit=request['limit'])
    if request != rebuilt:
        raise ValueError('Query record modified')
    retrieved = utc(retrieved_at)
    if retrieved < utc(request['as_of']):
        raise ValueError('Capture predates query cutoff')
    body = _json(raw)
    if not isinstance(body, dict) or body.get('type') != 'FeatureCollection':
        raise ValueError('Expected successful GeoJSON collection')
    metadata, features = body.get('metadata'), body.get('features')
    if (not isinstance(metadata,dict) or metadata.get('status') != 200
            or type(metadata.get('status')) is not int or not isinstance(features,list)):
        raise ValueError('Catalogue status/count mismatch')
    # Query responses can omit count, unlike the summary-feed example schema.
    if 'count' in metadata and (type(metadata['count']) is not int or metadata['count'] != len(features)):
        raise ValueError('Catalogue status/count mismatch')
    if ('limit' in metadata and (type(metadata['limit']) is not int or metadata['limit'] != request['limit'])
            or 'offset' in metadata and (type(metadata['offset']) is not int or metadata['offset'] != 1)):
        raise ValueError('Unexpected pagination metadata')
    server_url = metadata.get('url')
    if not isinstance(server_url,str):
        raise ValueError('Response query URL missing')
    parts = urlsplit(server_url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if (parts.scheme+'://'+parts.netloc+parts.path != ENDPOINT or parts.fragment
            or len(pairs) != len(dict(pairs)) or dict(pairs) != request['parameters']):
        raise ValueError('Response belongs to a different query')
    if len(features) >= request['limit']:
        raise SaturatedResponse('Result saturated; subdivide interval before counting')
    start, end = utc(request['selection']['start']), utc(request['selection']['end'])
    start_ms, end_ms = int(start.timestamp()*1000), int(end.timestamp()*1000)
    bounds, threshold = request['selection']['bounds'], request['selection']['min_magnitude']
    selected, excluded, identities = [], [], set()
    for index, feature in enumerate(features):
        if not isinstance(feature,dict) or feature.get('type') != 'Feature':
            raise ValueError('Invalid event feature')
        identity, props, geometry = feature.get('id'), feature.get('properties'), feature.get('geometry')
        if not isinstance(identity,str) or not identity or identity in identities:
            raise ValueError('Missing or duplicate event identity')
        identities.add(identity)
        if not isinstance(props,dict) or not isinstance(geometry,dict) or geometry.get('type') != 'Point':
            raise ValueError('Missing event properties/coordinates')
        coords = geometry.get('coordinates')
        if not isinstance(coords,list) or len(coords) != 3:
            raise ValueError('Invalid point coordinates')
        longitude, latitude = finite(coords[0],-180,180), finite(coords[1],-90,90)
        finite(coords[2],-100,1000)
        magnitude = finite(props.get('mag'),-10,15)
        time, updated = props.get('time'), props.get('updated')
        if (type(time) is not int or type(updated) is not int
                or not start_ms <= time <= end_ms or props.get('type') != 'earthquake'
                or not isinstance(props.get('magType'),str) or not props['magType']):
            raise ValueError('Invalid or out-of-query event data')
        reasons = []
        if time == end_ms:
            reasons.append('exclusive_end')
        if magnitude < threshold:
            reasons.append('below_exact_magnitude')
        if bounds is not None:
            west, east = bounds['west'], bounds['east']
            longitude_inside = west <= longitude <= east if west < east else longitude >= west or longitude <= east
            if not bounds['south'] <= latitude <= bounds['north'] or not longitude_inside:
                reasons.append('outside_exact_box')
        row = {'id':identity, 'source_pointer':'/features/'+str(index), 'time_ms':time,
               'updated_ms':updated, 'magnitude':magnitude, 'magnitude_type':props['magType'],
               'longitude':longitude, 'latitude':latitude}
        if reasons:
            excluded.append({'id':identity,'source_pointer':row['source_pointer'],'reasons':reasons})
        else:
            selected.append(row)
    return {'version':'usgs-capture-selection-1', 'request':rebuilt,
            'retrieved_at':retrieved.isoformat(), 'source_sha256':hashlib.sha256(raw).hexdigest(),
            'source_bytes':len(raw), 'response_count':len(features), 'selected_count':len(selected),
            'server_count_present':'count' in metadata,
            'events':selected, 'excluded':excluded, 'response_below_limit':True,
            'historical_vintage_verified':False, 'catalogue_completeness_in_nature_verified':False}


def window_counts(raw, request, *, retrieved_at, window_seconds):
    """Empirical complete-interval counts including zeros, with raw verification."""
    if type(window_seconds) is not int or not 3600 <= window_seconds <= 31 * 86400:
        raise ValueError('Window must be between one hour and 31 days')
    receipt = select_events(raw, request, retrieved_at=retrieved_at)
    start, end = utc(request['selection']['start']), utc(request['selection']['end'])
    start_ms, end_ms = int(start.timestamp()*1000), int(end.timestamp()*1000)
    width_ms = window_seconds * 1000
    if (end_ms - start_ms) % width_ms:
        raise ValueError('Partial exposure interval; choose exact complete windows')
    n = (end_ms - start_ms) // width_ms
    counts = [0] * n
    for event in receipt['events']:
        counts[(event['time_ms'] - start_ms) // width_ms] += 1
    mean = sum(counts) / n
    variance = math.fsum((count-mean)**2 for count in counts)/(n-1) if n > 1 else None
    return {'version':'usgs-window-counts-1', 'capture':receipt,
            'window_seconds':window_seconds, 'counts_including_zero':counts,
            'complete_windows':n, 'total_observed':sum(counts),
            'mean_per_window':mean, 'sample_variance':variance,
            'forecast_parameters_estimated':False,
            'caution':'Current revised catalogue; sparse/clustered data and parameter uncertainty require modelling'}
