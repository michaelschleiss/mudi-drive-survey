import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drive_app import Survey, position, qualify_probe, upload, upload_batch, request_measurement, percentile, handler, probe_worker, parse_radio, position_at
from drive_app import parse_uplink_layers, summarize_uplink
import mudi_survey


def fix(ts, lat=52, acc=5):
    return {'lat': lat, 'lon': 13, 'ts': ts, 'acc_m': acc}


class AcquisitionTests(unittest.TestCase):
    def test_uplink_layers_match_current_carriers_without_claiming_traffic(self):
        raw = '''+QCAINFO: "PCC",3749,50,"LTE BAND 8",1,363
+QCAINFO: "SCC",641760,11,"NR5G BAND 78",414
+QNWCFG: "lte_mimo_info",363,3749,1,1
+QNWCFG: "nr5g_mimo_info",414,641760,2,1
+QNWCFG: "nr5g_mimo_info",760,431070,1,0
+QNWCFG: "nr5g_mimo_info",1,12,2,65535'''
        sample = parse_uplink_layers(raw)
        self.assertEqual([c['band'] for c in sample['carriers']], ['B8', 'n78', None])
        self.assertFalse(sample['activity_confirmed'])
        self.assertFalse(sample['modem_sample_age_known'])
        later = parse_uplink_layers(raw.replace('414,641760,2,1', '415,641760,2,0'))
        summary = summarize_uplink([sample, later])
        nr = [c for c in summary['carriers'] if c['band'] == 'n78']
        self.assertEqual([(c['pci'], c['positive_samples']) for c in nr], [(414, 1), (415, 0)])
        self.assertEqual(parse_uplink_layers('ERROR')['carriers'], [])

    def test_carrier_table_reads_every_qcainfo_shape_and_keeps_signal(self):
        raw = ('+QCAINFO: "PCC",100,100,"LTE BAND 1",1,200,-105,-8,-74,8\n'
               '+QCAINFO: "SCC",9460,50,"LTE BAND 28",1,495,-101,-16,-76,253,0,-,-\n'
               '+QCAINFO: "SCC",631968,10,"NR5G BAND 78",287,-110,-11,745\n'
               '+QCAINFO: "SCC",372750,4,"NR5G BAND 3",1,620,0,-,-,-111,-16,-32768')
        carriers = mudi_survey.parse_carriers(raw)
        self.assertEqual([(c['band'], c['bw_mhz']) for c in carriers],
                         [('B1', 20), ('B28', 10), ('n78', 80), ('n3', 25)])
        # The 8-field NR line keeps PCI one place left of every other shape, and
        # the 12-field NR line pushes RSRP/RSRQ four places right. Reading either
        # with shared offsets would report a signal level as a PCI.
        self.assertEqual([c['pci'] for c in carriers], [200, 495, 287, 620])
        self.assertEqual([c['rsrp'] for c in carriers], [-105, -101, -110, -111])
        self.assertEqual([c['rsrq'] for c in carriers], [-8, -16, -11, -16])
        self.assertEqual([c['role'] for c in carriers], ['PCC', 'SCC', 'SCC', 'SCC'])

    def test_carrier_table_backs_the_ca_string_and_survives_unknown_shapes(self):
        raw = ('+QCAINFO: "PCC",3749,50,"LTE BAND 8",1,363\n'
               '+QCAINFO: "SCC",641760,11,"NR5G BAND 78",414')
        # An unrecognised field count still yields band and channel, so the CA
        # summary and the carrier count never silently lose a carrier.
        self.assertEqual(mudi_survey.parse_modem(raw)['ca'], 'B8(10)+n78(90)')
        self.assertEqual(mudi_survey.parse_modem(raw)['n_carriers'], 2)
        self.assertEqual([c['pci'] for c in mudi_survey.parse_carriers(raw)], [None, None])
        self.assertEqual(mudi_survey.parse_carriers('ERROR'), [])
        self.assertEqual(mudi_survey.parse_modem('ERROR')['ca'], '')

    def test_unmeasured_nr_leg_keeps_the_rat_but_invents_no_cell(self):
        # Captured verbatim: the NR leg is configured but has no measurement, so
        # the modem fills PCI with 0xFFFF, band with 0 and every level with "-".
        r = parse_radio('+QENG: "LTE","FDD",262,02,61F3B13,200,100,1,5,5,BBA2,-105,-8,-76,14,8,170,-\n'
                        '+QENG: "NR5G-NSA",262,02,65535,-,-,-,0,0,0,8')
        self.assertEqual(r['rat'], 'NR5G-NSA')          # the RAT is real
        self.assertIsNone(r['nr_band'])                 # the identity is not
        self.assertIsNone(r['nr_pci'])
        self.assertEqual((r['lte_band'], r['lte_rsrp']), ('B1', -105))
        survey = Survey(':memory:')
        try:
            survey.record('gps', {'lat': 48.1, 'lon': 11.2, 'acc_m': 5})
            survey.record('radio', dict(r, ts=time.time()))
            self.assertEqual([c['band'] for c in survey.snapshot(0)['cells']], ['B1'])
        finally:
            survey.db.close()

    def test_position_matches_time_not_average_of_irregular_fixes(self):
        fixes=[fix(100,52),fix(100.1,52.00001),fix(100.2,52.00002),fix(102,52.0002)]
        result=qualify_probe({'mbps':100,'error':''},fixes,100,102)
        self.assertAlmostEqual(result['lat'],52.0001)
        self.assertEqual(result['position_ts'],101)
        self.assertEqual(result['position_method'],'interpolated')
        self.assertEqual(len(result['path']),4)
        self.assertIsNone(position_at(fixes,103))
        self.assertIsNone(position_at([fix(100),fix(105)],102))

    def test_standalone_and_split_radio_lines(self):
        lte='"LTE","FDD",262,01,ABCD,123,1300,3,5,5,1,-85,-10,-60,20'
        for prefix in ['+QENG: ', '+QENG: "servingcell","NOCONN",']:
            parsed=parse_radio(prefix+lte)
            self.assertEqual((parsed['rat'],parsed['lte_band'],parsed['lte_sinr']),('LTE','B3',20))
        sa=parse_radio('+QENG: "servingcell","NOCONN","NR5G-SA","TDD",262,01,ABCD,321,1,630000,78,12,-80,-10,25,30')
        self.assertEqual((sa['rat'],sa['nr_band'],sa['nr_sinr']),('NR5G-SA','n78',25))

    def test_cell_identity_preserves_operator_cell_and_channel(self):
        r = parse_radio('+QENG: "LTE","FDD",262,01,ABCD,123,1300,3,5,5,FACE,-85,-10,-60,20\n'
                        '+QENG: "NR5G-NSA",262,01,456,-80,25,-9,640000,78,12,1')
        self.assertEqual((r['lte_plmn'], r['lte_cell_id'], r['lte_arfcn'], r['lte_tac']),
                         ('262-01', 'ABCD', 1300, 'FACE'))
        self.assertEqual((r['nr_plmn'], r['nr_arfcn'], r['nr_band']), ('262-01', 640000, 'n78'))
        self.assertNotIn('nr_cell_id', r)  # NSA does not report a global NR cell ID here.
        sa = parse_radio('+QENG: "servingcell","NOCONN","NR5G-SA","TDD",262,01,123ABCD,123,ABC,640000,78,12,-80,-9,25,1')
        self.assertEqual(sa['nr_cell_id'], '123ABCD')

    def test_cell_inventory_keeps_real_ids_distinct_and_rejects_stale_gps(self):
        with tempfile.TemporaryDirectory() as folder:
            s = Survey(Path(folder)/'survey.db')
            s.record('gps', fix(100))
            r = {'ts': 101, 'rat': 'LTE', 'lte_band': 'B3', 'lte_pci': 1,
                 'lte_plmn': '262-01', 'lte_cell_id': 'AAAA', 'lte_arfcn': 1300,
                 'lte_rsrp': -90, 'lte_sinr': 10}
            s.record('radio', r)
            s.record('radio', dict(r, lte_cell_id='BBBB'))
            s.record('radio', dict(r, ts=110, lte_rsrp=-50))
            cells = s.snapshot(0)['cells']
            self.assertEqual(len(cells), 2)
            a = next(c for c in cells if c['cell_id'] == 'AAAA')
            self.assertEqual((a['observations'], a['located'], a['best_rsrp']), (2, 1, -50))
            self.assertEqual(a['best_position']['rsrp'], -90)
            s.db.close()
            restored = Survey(Path(folder)/'survey.db')
            self.assertEqual(restored.snapshot(0)['cells'], cells)
            self.assertFalse(restored.running)
            self.assertEqual(restored.mode, 'parked')
            restored.db.close()

    def test_all_band_hunt_uses_coincident_evidence_not_band_name(self):
        s = Survey(':memory:')
        s.record('gps', fix(100))
        common = {'ts': 101, 'rat': 'NR5G-NSA', 'nr_pci': 1, 'nr_plmn': '262-01'}
        s.record('radio', dict(common, nr_band='n78', nr_bw=100, nr_rsrp=-120, nr_sinr=0))
        s.record('radio', dict(common, nr_band='n1', nr_bw=20, nr_rsrp=-90, nr_sinr=15,
                              ca='B3(20)+n1(20)', lte_cell_id='ABC', lte_band='B3'))
        cells = s.snapshot(0)['cells']
        self.assertEqual(cells[0]['band'], 'n1')
        self.assertEqual(cells[0]['hunt_priority'], 2)
        candidate = cells[0]['candidate_position']
        self.assertEqual((candidate['rsrp'], candidate['sinr'], candidate['lte_anchor']), (-90, 15, 'ABC'))
        s.record('radio', dict(common, nr_band='n78', nr_bw=100, nr_rsrp=-80, nr_sinr=0))
        s.record('radio', dict(common, nr_band='n78', nr_bw=100, nr_rsrp=-120, nr_sinr=25))
        n78 = next(c for c in s.snapshot(0)['cells'] if c['band']=='n78')
        self.assertEqual(n78['hunt_priority'], 1)  # Separate signal peaks cannot create a green candidate.
        s.db.close()

    def test_radio_track_matches_timestamps_and_restores_without_raw_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'survey.db'
            survey = Survey(path)
            survey.record('gps', fix(100))
            r = {'ts': 102, 'rat': 'LTE', 'lte_band': 'B3', 'lte_pci': 1,
                 'lte_rsrp': -90, 'lte_rsrq': -10, 'raw_radio': 'raw diagnostic text'}
            event = survey.record('radio', r)
            survey.record('radio', dict(r, ts=104))
            points = survey.snapshot(0)['observations']
            self.assertEqual(len(points), 1)
            self.assertEqual((points[0]['gps_ts'], points[0]['gps_delta_s']), (100, 2))
            self.assertEqual(points[0]['radio']['lte_rsrq'], -10)
            self.assertNotIn('raw_radio', points[0]['radio'])
            self.assertEqual(survey.snapshot(event['id'])['observations'], [])
            survey.db.close()
            restored = Survey(path)
            self.assertEqual(restored.snapshot(0)['observations'], points)
            restored.db.close()

    def test_request_estimate_uses_ttfb_not_server_subtraction(self):
        info = {'http_code': 200, 'exitcode': 0, 'size_upload': 1000000,
                'time_total': 1.3, 'time_pretransfer': .2, 'time_starttransfer': 1.2, 'num_connects': 0}
        result = request_measurement(info, 1000000, 'Server-Timing: cfReqDur;dur=900', now=123)
        self.assertAlmostEqual(result['estimate_mbps'], 8.04)
        self.assertEqual(result['server_reported_ms'], 900)
        self.assertAlmostEqual(result['payload_mbps'], 8/1.3)
        self.assertTrue(result['connection_reused'])
        self.assertIsNone(request_measurement(dict(info, exitcode=28), 1000000)['estimate_mbps'])
        self.assertIsNone(request_measurement(dict(info, size_upload=100), 1000000)['estimate_mbps'])
        self.assertIsNone(request_measurement(dict(info, time_starttransfer=.201), 1000000)['estimate_mbps'])
        self.assertEqual(percentile([10, 20, 30], .5), 20)
        self.assertEqual(percentile([10, 20, 30], .9), 28)
        self.assertIsNone(percentile([], .9))

    def test_hunt_target_persists_and_is_not_replaced_by_serving_cell(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'survey.db'
            s=Survey(path)
            s.record('radio', {'rat':'LTE', 'lte_band':'B3', 'lte_pci':1, 'lte_plmn':'262-01', 'lte_cell_id':'AAA'})
            identity=next(iter(s.cells))
            s.record('hunt_target', {'identity':identity})
            s.record('radio', {'rat':'LTE', 'lte_band':'B1', 'lte_pci':2, 'lte_plmn':'262-01', 'lte_cell_id':'BBB'})
            self.assertEqual(s.snapshot(0)['target'], identity)
            s.db.close()
            restored=Survey(path)
            self.assertEqual(restored.snapshot(0)['target'], identity)
            restored.record('hunt_target', {'identity':None})
            self.assertIsNone(restored.snapshot(0)['target'])
            restored.db.close()

    def test_gps_invalid_values_and_original_timestamp(self):
        for p in [fix(100, float('nan')), fix(100, 91), fix(100, acc=-1), fix(60), fix(103)]:
            with self.assertRaises(ValueError):
                position(p, now=100)
        self.assertEqual(position(fix(99), now=100)['ts'], 99)

    def test_retransmitting_fix_does_not_refresh_gps(self):
        s = Survey(':memory:')
        p = fix(time.time())
        self.assertTrue(s.gps(p))
        self.assertFalse(s.gps(p))
        self.assertFalse(s.gps(fix(p['ts']-1)))
        self.assertEqual(s.seq, 1)

    def test_probe_footprint_and_stale_inaccurate_missing_gps(self):
        result = {'mbps': 100, 'error': ''}
        valid = qualify_probe(result, [fix(100), fix(101,52.0001),fix(102,52.0002)],100,102)
        self.assertTrue(valid['eligible'])
        self.assertAlmostEqual(valid['lat'],52.0001)
        for fixes in [[], [fix(90)], [fix(100,acc=100)], [fix(100),fix(105)], [fix(100),fix(102,52.002)]]:
            self.assertFalse(qualify_probe(result, fixes, 100,105)['eligible'])
        self.assertFalse(qualify_probe({'error':'HTTP 400'},[fix(100),fix(102)],100,102)['eligible'])

    def test_ranking_uses_only_qualified_probes_and_lower_quartile(self):
        s=Survey(':memory:')
        for value in [10,20,1000]:
            s.record('probe',dict(fix(100),eligible=True,mbps=value))
        s.record('traffic',dict(fix(100),mbps=10000))
        s.record('probe',dict(fix(100),eligible=False,mbps=20000))
        best=s.snapshot(0)['best'][0]
        self.assertEqual((best['n'],best['median'],best['floor'],best['confidence']),(3,20,10,'Repeated'))

    def test_ring_cursor_keeps_increasing_and_restart_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'survey.db'
            s=Survey(path)
            from collections import deque
            s.events=deque(maxlen=3)
            for i in range(8): s.record('traffic',{'mbps':i})
            self.assertTrue(s.snapshot(2)['reset'])
            self.assertEqual([e['id'] for e in s.snapshot(6)['events']],[7,8])
            self.assertTrue(s.snapshot(20)['reset'])
            s.db.close()
            s=Survey(path)
            self.assertEqual(s.seq,8)
            s.record('traffic',{'mbps':8})
            self.assertEqual(s.seq,9)
            s.db.close()

    def test_upload_rejects_error_partial_and_too_short(self):
        with tempfile.TemporaryDirectory() as folder:
            for code,sent,duration,rc,valid in [(200,100,1,0,True),(400,100,1,0,False),(200,50,1,0,False),(200,100,.1,0,False),(200,100,1,28,False)]:
                output=SimpleNamespace(stdout=json.dumps({'http_code':code,'size_upload':sent,'time_total':duration}),returncode=rc)
                with patch('drive_app.subprocess.run',return_value=output) as run:
                    result=upload('https://example.org/up','en12',100,Path(folder)/'payload')
                    self.assertEqual(not bool(result['error']),valid)
                    self.assertIn('--interface',run.call_args.args[0])

    def test_slow_probe_does_not_block_gps_or_snapshot(self):
        s=Survey(':memory:');s.running=True;stop=threading.Event();entered=threading.Event();release=threading.Event()
        def slow(*a, **kw):
            entered.set();release.wait(2);return {'mbps':10,'bytes':100,'error':''}
        config=SimpleNamespace(upload_url='https://example.org',iface='en12',probe_every=4,upload_method='POST')
        with patch('drive_app.upload_batch',side_effect=slow):
            t=threading.Thread(target=probe_worker,args=(s,stop,config));t.start()
            try:
                self.assertTrue(entered.wait(1))
                start=time.monotonic();s.gps(fix(time.time()));snapshot=s.snapshot(0)
                self.assertLess(time.monotonic()-start,.2)
                self.assertTrue(snapshot['busy'])
                self.assertEqual(snapshot['latest']['gps']['lat'],52)
            finally:
                stop.set();release.set();t.join(3)

    def test_warmed_batch_reuses_connection_and_counts_only_measured_bytes(self):
        peers = []
        sizes = []
        reject = []
        class Sink(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args): pass
            def do_POST(self):
                size = int(self.headers['Content-Length'])
                peers.append(self.client_address)
                sizes.append(size)
                self.rfile.read(size)
                self.send_response(503 if reject and len(sizes) > 1 else 200)
                self.send_header('Content-Length', '0')
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Sink)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                progress = []
                result = upload_batch(f'http://127.0.0.1:{server.server_port}/up',
                                      'lo0' if sys.platform == 'darwin' else 'lo',
                                      1048576, Path(folder)/'payload', 'POST', 5, progress.append)
            self.assertEqual(result['error'], '')
            self.assertEqual(sizes, [262144, 1048576, 1048576])
            self.assertEqual(len(set(peers)), 1)
            self.assertEqual(result['reused_connections'], 2)
            self.assertEqual(result['payload_bytes'], 2097152)
            self.assertEqual(result['bytes'], 2359296)
            self.assertEqual(len(progress), 2)
            self.assertEqual(len(result['request_measurements']), 2)
            self.assertEqual(len(progress[-1]['requests']), 2)
            self.assertTrue(result['short'])
            self.assertAlmostEqual(result['mbps'], 2097152*8/1e6/result['transfer_seconds'])
            reject.append(True); sizes.clear()
            with tempfile.TemporaryDirectory() as folder:
                failed = upload_batch(f'http://127.0.0.1:{server.server_port}/up',
                                      'lo0' if sys.platform == 'darwin' else 'lo',
                                      1048576, Path(folder)/'payload', 'POST', 5)
            self.assertIn('HTTP 503', failed['error'])
            self.assertEqual(len(sizes), 2)  # Failed request stops the rest of the batch.
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_failure_penalty_and_short_or_moving_parked_exclusion(self):
        s = Survey(':memory:')
        fixes = [fix(100), fix(101), fix(102)]
        for result in [{'mbps': 100, 'error': ''}, {'mbps': None, 'error': 'timeout'}]:
            s.record('probe', qualify_probe(result, fixes, 100, 102))
        best = s.snapshot(0)['best'][0]
        self.assertEqual((best['n'], best['failed'], best['floor'], best['success_rate']), (2, 1, 0, .5))
        short = qualify_probe({'mbps': 100, 'short': True}, fixes, 100, 102)
        moving = qualify_probe({'mbps': 100, 'mode': 'parked'},
                               [fix(100), fix(101, 52.0002), fix(102, 52.0004)], 100, 102)
        self.assertFalse(short['eligible'])
        self.assertFalse(moving['eligible'])

    def test_parked_verification_stops_after_one_attempt(self):
        s = Survey(':memory:'); s.running = True; s.mode = 'parked'; s.remaining = 1
        stop = threading.Event()
        config = SimpleNamespace(upload_url='https://example.org', iface='en12', probe_every=0, upload_method='POST')
        with patch('drive_app.upload_batch', return_value={'mbps': 10, 'bytes': 100, 'error': ''}) as batch:
            worker = threading.Thread(target=probe_worker, args=(s, stop, config))
            worker.start()
            deadline = time.monotonic()+2
            while s.running and time.monotonic() < deadline:
                time.sleep(.01)
            stop.set(); worker.join(2)
            self.assertFalse(s.running)
            self.assertEqual(s.remaining, 0)
            self.assertEqual(batch.call_count, 1)
            self.assertTrue(all(call.args[5] == 20 for call in batch.call_args_list))

    def test_http_controls_export_and_input_validation(self):
        s=Survey(':memory:')
        config=SimpleNamespace(demo=True,upload_url='',probe_every=4,radio_interval=1,iface='en12')
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(s,config));thread=threading.Thread(target=server.serve_forever);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            data=json.dumps(fix(time.time())).encode()
            with urlopen(Request(base+'/api/pos',data=data)) as r:self.assertTrue(json.load(r)['accepted'])
            with urlopen(base+'/api/state') as r:self.assertEqual(json.load(r)['cursor'],1)
            with urlopen(base+'/api/export') as r:self.assertIn(b'"lat"',r.read().replace(b'""',b'"'))
            for req in [Request(base+'/api/pos',data=b'{'), Request(base+'/api/pos',data=b'[]'),
                        Request(base+'/api/control',data=b'{"running":true}',headers={'Origin':'https://bad.example'})]:
                with self.assertRaises(HTTPError) as caught:
                    urlopen(req)
                caught.exception.close()
            with urlopen(Request(base+'/api/control',data=b'{"running":true}')) as r:self.assertTrue(json.load(r)['ok'])
            self.assertTrue(s.running)
            s.record('radio', {'rat':'LTE', 'lte_band':'B3', 'lte_pci':1})
            identity=next(iter(s.cells))
            with urlopen(Request(base+'/api/target', data=json.dumps({'identity':identity}).encode())) as r:
                self.assertEqual(json.load(r)['target'], identity)
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(base+'/api/target', data=b'{"identity":"unknown"}'))
            caught.exception.close()
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
