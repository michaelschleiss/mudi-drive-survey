"""Import public BNetzA EMF sites in a radius into a compact JSON/CSV layer.

The EMF map is public but has no bulk download. This uses its own web session,
the documented map tile endpoint, and the public detail page. Coordinates remain
the BNetzA-provided approximate positions (up to 80 m uncertainty).
"""
import argparse
import base64
import csv
import json
import math
import re
import time
from html import unescape
from pathlib import Path

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = 'https://www.bundesnetzagentur.de'
MAP = ROOT + '/DE/Vportal/TK/Funktechnik/EMF/start.html'
JS = ROOT + '/emf-karte/js.asmx/jscontent?set=gsb2021'
SITES = ROOT + '/emf-karte/Standortservice.asmx/GetStandorteFreigabe'
SMALL_CELLS = ROOT + '/emf-karte/Standortservice.asmx/GetStandorteSmallCellFreigabe'
DETAIL = ROOT + '/emf-karte/hf.aspx'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': MAP,
           'Origin': ROOT, 'Accept': 'application/json'}


def distance_m(a, b, c, d):
    x = math.radians(c-a); y = math.radians(d-b)
    v = math.sin(x/2)**2 + math.cos(math.radians(a))*math.cos(math.radians(c))*math.sin(y/2)**2
    return 6371008.8 * 2 * math.asin(math.sqrt(v))


def decrypt(password, value):
    key = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16,
        salt=b'cryptography123example', iterations=1000, backend=default_backend()).derive(password.encode())
    plain = Cipher(algorithms.AES(key), modes.CBC(bytes.fromhex('a5a8d2e9c1721ae0e84ad660c472b1f3')),
                   backend=default_backend()).decryptor().update(base64.b64decode(value))
    return json.loads(plain[:-plain[-1]].decode())


def text(value):
    return re.sub(r'<[^>]+>', '', unescape(value)).strip()


def detail(session, fid):
    page = session.get(DETAIL, params={'fid': fid}, headers={'User-Agent': HEADERS['User-Agent'], 'Referer': MAP}, timeout=20).text
    def field(id_):
        found = re.search(r'<div[^>]+id=["\']'+re.escape(id_)+r'["\'][^>]*>(.*?)</div>', page, re.S|re.I)
        return text(found.group(1)) if found else ''
    providers = sorted(set(re.findall(r'<img[^>]+(?:alt=["\']([^"\']+)["\'])',
                                      re.search(r'<div[^>]+id=["\']div_mobilfunkanbieter["\'][^>]*>(.*?)</div>', page, re.S|re.I).group(1)
                                      if re.search(r'<div[^>]+id=["\']div_mobilfunkanbieter["\'][^>]*>(.*?)</div>', page, re.S|re.I) else '', re.I)))
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', re.search(r'<div[^>]+id=["\']div_sendeantennen["\'][^>]*>(.*?)</table>', page, re.S|re.I).group(1)
                      if re.search(r'<div[^>]+id=["\']div_sendeantennen["\'][^>]*>(.*?)</table>', page, re.S|re.I) else '', re.S|re.I)[1:]
    antennas = []
    for row in rows:
        cells = [text(x) for x in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S|re.I)]
        if len(cells) == 5:
            antennas.append({'type': cells[0], 'height_m': cells[1], 'azimuth_deg': cells[2],
                             'safety_main_m': cells[3], 'safety_vertical_m': cells[4]})
    number = field('standortbnr')
    return {'site_certificate_id': re.sub(r'\s+', ' ', number).split()[-1] if number else str(fid),
            'operators': providers, 'antennas': antennas}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--lat', type=float, required=True); ap.add_argument('--lon', type=float, required=True)
    ap.add_argument('--radius-km', type=float, default=10); ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(); radius = args.radius_km * 1000
    session = requests.Session(); session.get(MAP, headers=HEADERS, timeout=20)
    script = session.get(JS, headers=HEADERS, timeout=20).text
    password = re.search(r'var c=CryptoJS\.enc\.Utf8\.parse\("(.*?)"\);', script).group(1)
    lat_step = .04; lon_step = .04 / max(.2, math.cos(math.radians(args.lat)))
    points = {}
    south, north = args.lat-radius/111195, args.lat+radius/111195
    west, east = args.lon-radius/(111195*math.cos(math.radians(args.lat))), args.lon+radius/(111195*math.cos(math.radians(args.lat)))
    lat = south
    while lat < north:
        lon = west
        while lon < east:
            box = {'Box': {'sued': lat, 'west': lon, 'nord': min(north,lat+lat_step), 'ost': min(east,lon+lon_step)}}
            for kind, endpoint in (('fixed_site', SITES), ('small_cell', SMALL_CELLS)):
                data = session.post(endpoint, headers={**HEADERS, 'Content-Type': 'application/json'}, data=json.dumps(box), timeout=30).json()['d']
                if data.get('SecMode'): data = decrypt(password, data['Result'])
                for site in data:
                    if distance_m(args.lat,args.lon,site['Lat'],site['Lng']) <= radius:
                        site['kind'] = kind
                        points[f'{kind}:{site["fID"]}'] = site
            lon += lon_step
            time.sleep(.12)
        lat += lat_step
    sites=[]
    for n, site in enumerate(points.values(), 1):
        row = detail(session, site['fID']) if site['kind'] == 'fixed_site' else {
            'site_certificate_id': str(site['fID']), 'operators': [], 'antennas': []}
        row.update(fid=site['fID'], kind=site['kind'], lat=site['Lat'], lon=site['Lng'], title=site.get('Titel',''))
        sites.append(row); time.sleep(.12)
        print(f'{n}/{len(points)} {row["site_certificate_id"]}', flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({'source': 'Bundesnetzagentur EMF Karte', 'center': {'lat':args.lat,'lon':args.lon},
        'radius_km':args.radius_km, 'position_uncertainty_m':80, 'sites':sites}, ensure_ascii=False, indent=2))
    with args.out.with_suffix('.csv').open('w', newline='') as f:
        w=csv.DictWriter(f, fieldnames=['site_certificate_id','kind','lat','lon','operators','antenna_count','azimuths_deg']); w.writeheader()
        for s in sites: w.writerow({'site_certificate_id':s['site_certificate_id'],'lat':s['lat'],'lon':s['lon'],
            'kind':s['kind'],'operators':'; '.join(s['operators']),'antenna_count':len(s['antennas']),
            'azimuths_deg':'; '.join(str(a['azimuth_deg']) for a in s['antennas'])})

if __name__ == '__main__': main()
