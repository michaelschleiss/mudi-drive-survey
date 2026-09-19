"""Conservative grid candidate areas; not a calibrated confidence estimator.

Caller must supply observations for ONE confirmed cell/continuity identity.
Uncertainty is an interval allowance, not a standard deviation. Defaults are
engineering assumptions awaiting field calibration. No radio decoding occurs here.
"""
import math
import statistics


def estimate_candidate_area(observations, *, model_error_m=100.0,
                            cluster_m=25.0, grid_m=25.0, max_grid_side=121):
    """Accept lat/lon/range_m plus acc_m, step_m or uncertainty_m.

    At most 20 percent of spatial clusters may disagree (only with >=5 clusters).
    Grid cells satisfying that consensus are returned as separate connected regions.
    Collinear/stationary data never produces a candidate marker.
    """
    if (not all(math.isfinite(v) for v in (model_error_m, cluster_m, grid_m))
            or model_error_m < 0 or cluster_m <= 0 or grid_m <= 0
            or not isinstance(max_grid_side, int) or not 3 <= max_grid_side <= 501):
        raise ValueError('Invalid estimator configuration')
    usable = []
    for row in observations:
        try:
            lat, lon, radius = (float(row[k]) for k in ('lat', 'lon', 'range_m'))
            acc = float(row.get('acc_m', 0))
            quant = float(row['uncertainty_m']) if 'uncertainty_m' in row else float(row.get('step_m', 0))/2
            if not all(math.isfinite(v) for v in (lat, lon, radius, acc, quant)):
                continue
            if abs(lat) >= 89 or abs(lon) > 180 or min(radius, acc, quant) < 0:
                continue
            usable.append((lat, lon, radius, acc+quant+model_error_m))
        except (KeyError, TypeError, ValueError):
            continue
    result = dict(status='insufficient_observations', calibrated=False,
                  interpretation='candidate area; not a confidence probability',
                  model_error_m=model_error_m, independent_positions=0,
                  regions=[], candidate_marker=None)
    result['accepted_observations'] = len(usable)
    if not usable:
        return result
    # Stabilize cluster formation under packet/replay ordering.
    usable.sort()
    lat0, lon0 = usable[0][:2]
    scale = 111320.0
    east_scale = scale*math.cos(math.radians(lat0))
    def geographic(x, y):
        return dict(lat=lat0+y/scale, lon=lon0+x/east_scale)
    groups = []
    for lat, lon, radius, allowance in usable:
        x, y = (lon-lon0)*east_scale, (lat-lat0)*scale
        group = next((g for g in groups if math.hypot(x-g[0][0], y-g[0][1]) <= cluster_m), None)
        if group is None:
            groups.append([(x, y, radius, allowance)])
        else:
            group.append((x, y, radius, allowance))
    # Repeated packets at one location cannot increase its vote. Preserve the
    # union of its observed range intervals to avoid false precision from repeats.
    points = []
    for group in groups:
        x = statistics.median(v[0] for v in group)
        y = statistics.median(v[1] for v in group)
        lo = min(v[2]-v[3]-math.hypot(v[0]-x, v[1]-y) for v in group)
        hi = max(v[2]+v[3]+math.hypot(v[0]-x, v[1]-y) for v in group)
        points.append((x, y, max(0, lo), hi))
    n = len(points)
    result['independent_positions'] = n
    if n < 3:
        return result
    mx, my = (sum(p[k] for p in points)/n for k in (0, 1))
    xx = sum((p[0]-mx)**2 for p in points)/n
    yy = sum((p[1]-my)**2 for p in points)/n
    xy = sum((p[0]-mx)*(p[1]-my) for p in points)/n
    disc = math.sqrt((xx-yy)**2+4*xy*xy)
    major, minor = (xx+yy+disc)/2, max(0, (xx+yy-disc)/2)
    geometry_ok = major > cluster_m**2 and minor/max(major, 1) >= .05
    result['geometry'] = 'diverse' if geometry_ok else 'limited'
    # Union bounds deliberately retain hypotheses when some observations disagree.
    left = min(x-hi for x,y,lo,hi in points)
    right = max(x+hi for x,y,lo,hi in points)
    bottom = min(y-hi for x,y,lo,hi in points)
    top = max(y+hi for x,y,lo,hi in points)
    step = max(grid_m, (right-left)/(max_grid_side-1), (top-bottom)/(max_grid_side-1))
    nx, ny = math.ceil((right-left)/step)+1, math.ceil((top-bottom)/step)+1
    allowance = step/math.sqrt(2)  # Any point within a displayed grid square.
    rejects = int(n*.2) if n >= 5 else 0
    feasible = set()
    for i in range(nx):
        for j in range(ny):
            x, y = left+i*step, bottom+j*step
            failures = 0
            for px, py, lo, hi in points:
                d = math.hypot(x-px, y-py)
                if d < lo-allowance or d > hi+allowance:
                    failures += 1
                    if failures > rejects:
                        break
            if failures <= rejects:
                feasible.add((i,j))
    result.update(grid_m=step, rejected_clusters_allowed=rejects)
    while feasible:
        seed = min(feasible)
        feasible.remove(seed)
        component, queue = [seed], [seed]
        while queue:
            i,j = queue.pop()
            for di,dj in ((1,0),(-1,0),(0,1),(0,-1)):
                neighbor = i+di,j+dj
                if neighbor in feasible:
                    feasible.remove(neighbor)
                    queue.append(neighbor)
                    component.append(neighbor)
        cells = [geographic(left+i*step, bottom+j*step) for i,j in component]
        polygons = []
        for i,j in component:
            x,y = left+i*step, bottom+j*step
            corners = [geographic(x+dx*step/2,y+dy*step/2)
                       for dx,dy in ((-1,-1),(1,-1),(1,1),(-1,1),(-1,-1))]
            polygons.append([[p['lat'],p['lon']] for p in corners])
        result['regions'].append(dict(cells=cells, polygons=polygons, cell_size_m=step,
                                     area_m2=len(cells)*step**2))
    result['status'] = ('inconsistent_ranges' if not result['regions'] else
                        'candidate_area' if geometry_ok else 'needs_different_direction')
    # A marker is only a visual representative, never a claimed mast fix.
    if geometry_ok and len(result['regions']) == 1:
        cells = result['regions'][0]['cells']
        centre = {k: sum(c[k] for c in cells)/len(cells) for k in ('lat','lon')}
        result['candidate_marker'] = min(cells, key=lambda c:
            ((c['lat']-centre['lat'])*scale)**2+((c['lon']-centre['lon'])*east_scale)**2)
    return result
