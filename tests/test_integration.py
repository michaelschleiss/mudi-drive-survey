import json
import math
from pathlib import Path
import sqlite3
import struct
import tempfile
import time
import unittest
from drive_app import Survey, parse_radio
from cell_evidence import fit_rings, load_sites, match_sites
from diagnostic_timing import LTEAdvance, commands, rach
from survey_core import meters


def gps(ts):return {'ts':ts,'lat':52.52,'lon':13.4,'acc_m':5,'speed_kmh':0,'source':'fixture'}
def radio(ts, cell='ABC', **extras):return {'ts':ts,'rat':'LTE','lte_plmn':'262-01','lte_cell_id':cell,'lte_pci':123,'lte_band':'B3','lte_rsrp':-85,**extras}
def timing(stamp, **extras):return {'ts':stamp,'key':'lte:262-01:ABC','reference_key':'lte:262-01:ABC','ta_index':10,'encoding':'lte-absolute-16ts','source':'fixture',**extras}
def rach_body(value):return bytes([1,1,0,0])+struct.pack('<BBH',6,2,11)+bytes([0,0,0,2])+struct.pack('<HBHH',0,0,123,value)
def command_body(value, tag=0):return bytes([0x32,0,0,0,1,0,0,0])+struct.pack('<LLLBBH',0,0,0,0,1,0)+(1+(29<<1)).to_bytes(3,'little')+bytes([(tag<<6)|value])+bytes(8)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.s=Survey(Path(self.folder.name)/'session.sqlite3')
        self.stamp=time.time()
        self.s.gps(gps(self.stamp))
        self.s.record('radio',radio(self.stamp))
    def tearDown(self):
        self.s.db.close();self.folder.cleanup()

    def test_full_identity_and_unknown_sightings(self):
        self.s.record('radio',radio(self.stamp,'000abc'))
        self.s.record('radio',radio(self.stamp,'DEF'))
        self.s.record('radio',radio(self.stamp,None))
        self.s.record('radio',radio(self.stamp,None))
        snap=self.s.snapshot(0)
        self.assertEqual(len(snap['cells']),2)
        self.assertEqual(self.s.cells['lte:262-01:ABC']['observations'],2)
        self.assertEqual(len(snap['unresolved']),1)
        self.assertIsNone(snap['unresolved'][0]['key'])
        self.assertEqual(len(snap['observations']),5)

    def test_timing_reference_validation_and_resume(self):
        for extras in [{'reference_key':'nr:262-01:ABC'},{'encoding':'delta'},{'ta_index':-1},{'ts':self.stamp-40}]:
            with self.assertRaises(ValueError):self.s.timing(timing(self.stamp,**extras))
        self.assertTrue(self.s.timing(timing(self.stamp)))
        self.assertFalse(self.s.timing(timing(self.stamp)))
        self.s.record('radio',{'ts':self.stamp,'rat':'NR5G-SA','nr_band':'n78','nr_plmn':'262-01','nr_cell_id':'ABC','nr_pci':123})
        self.assertEqual(len(self.s.evidence.detail('nr:262-01:ABC')['rings']),0)
        self.s.db.close()
        restored=Survey(Path(self.folder.name)/'session.sqlite3')
        try:
            self.assertEqual(restored.evidence.detail('lte:262-01:ABC')['rings'],self.s.evidence.detail('lte:262-01:ABC')['rings'])
            self.assertFalse(restored.running)
        finally:restored.db.close()

    def test_slow_disk_does_not_block_gps(self):
        db=sqlite3.connect(self.s.journal.path)
        db.execute('BEGIN IMMEDIATE')
        try:
            start=time.monotonic()
            for i in range(100):self.s.record('gps',gps(self.stamp+i*.001))
            self.assertLess(time.monotonic()-start,.25)
            self.assertEqual(self.s.seq,102)
        finally:db.rollback();db.close()

    def test_stale_slow_or_inaccurate_positions_not_mapped(self):
        base=len(self.s.radio_track)
        self.s.record('radio',radio(self.stamp+2))
        self.s.record('radio',radio(self.stamp,poll_ms=1600))
        self.s.record('gps',dict(gps(self.stamp+3),acc_m=40))
        self.s.record('radio',radio(self.stamp+3))
        self.assertEqual(len(self.s.radio_track),base)

    def test_nr_width_and_sinr_not_fabricated(self):
        raw='+QENG: "servingcell","CONNECT","NR5G-SA","TDD",262,01,ABC,123,ABC,640000,78,12,-80,-9,25,1'
        r=parse_radio(raw)
        self.assertEqual(r['nr_bw'],100)
        self.assertIsNone(r['nr_sinr'])
        self.assertEqual(r['nr_sinr_raw'],25)
        self.assertIsNone(parse_radio(raw.replace('CONNECT','NOCONN'))['nr_bw'])

    def test_parked_gate_and_storage_failure(self):
        self.s.require_parked()
        for extra in [{'speed_kmh':30},{'speed_kmh':None},{'acc_m':80},{'ts':self.stamp-20}]:
            self.s.record('gps',gps(self.stamp)|extra)
            with self.assertRaises(ValueError):self.s.require_parked()
        self.s.record('gps',gps(time.time()))
        self.s.journal.error='Disk full'
        with self.assertRaises(ValueError):self.s.require_parked()
        self.s.journal.error=''

    def test_upload_budget_stops_before_sending(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import threading
        from drive_app import probe_worker
        self.s.bytes=1048576;self.s.running=True;self.s.remaining=3
        stop=threading.Event()
        config=SimpleNamespace(budget_mb=1,upload_url='https://example.test',iface='en12',probe_every=0,upload_method='POST')
        with patch('drive_app.upload_batch') as upload:
            thread=threading.Thread(target=probe_worker,args=(self.s,stop,config));thread.start()
            try:
                until=time.monotonic()+1
                while self.s.running and time.monotonic()<until:time.sleep(.01)
                self.assertFalse(self.s.running);upload.assert_not_called()
                self.assertIn('budget',self.s.error)
            finally:stop.set();thread.join(2)

    def test_legacy_sqlite_payload_migration(self):
        path=Path(self.folder.name)/'legacy.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE events (id INTEGER PRIMARY KEY, kind TEXT, ts REAL, data TEXT)')
            db.execute('INSERT INTO events VALUES (1,?,?,?)',('gps',self.stamp,json.dumps(gps(self.stamp))))
        s=Survey(path)
        try:
            self.assertEqual(s.seq,1)
            self.assertEqual(s.record('radio',radio(self.stamp))['id'],2)
        finally:s.db.close()

    def test_geometry_and_site_candidates(self):
        tower={'lat':52.52,'lon':13.4};rings=[]
        for i in range(12):
            angle=i*math.tau/12
            p={'lat':52.52+.004*math.sin(angle),'lon':13.4+.007*math.cos(angle)}
            rings.append(p|{'distance_m':meters(p,tower),'uncertainty_m':60})
        estimate=fit_rings(rings)
        self.assertEqual(estimate['status'],'tentative');self.assertLess(meters(estimate,tower),25)
        self.assertEqual(fit_rings(rings[:3])['status'],'insufficient')
        self.assertEqual(fit_rings([r|{'lat':52.52} for r in rings])['status'],'poor_geometry')
        path=Path(self.folder.name)/'sites.csv';path.write_text('id,lat,lon,operator\nA,52.52,13.4,262-01\nB,52.52,13.4,262-02\n')
        matches=match_sites(rings,load_sites(path),'lte:262-01:ABC')
        self.assertEqual([p['id'] for p in matches],['A'])
        self.assertEqual(matches[0]['status'],'candidate')


class DiagnosticTests(unittest.TestCase):
    def record(self, sequence=0, code='B062', body=None, **extras):
        return {'key':'lte:262-01:ABC','reference_key':'lte:262-01:ABC','epoch':'attachment-1',
                'tag_id':0,'sequence':sequence,'ts':100+sequence*.1,'code':code,
                'body_hex':(body or rach_body(10)).hex(),**extras}

    def test_legacy_decode_with_tag_retention(self):
        self.assertEqual(rach(rach_body(10)),10)
        self.assertEqual(commands(command_body(32,2)),[(2,32)])
        d=LTEAdvance();self.assertEqual(d.feed(self.record())['ta_index'],10)
        self.assertEqual(d.feed(self.record(1,'B063',command_body(32)))['ta_index'],11)
        self.assertIsNone(d.feed(self.record(2,'B063',command_body(32,1))))
        self.assertEqual(d.value,11)

    def test_loss_epoch_and_identity_changes_require_new_absolute_ta(self):
        for change in [{'sequence':3},{'epoch':'attachment-2'},
                       {'key':'lte:262-01:DEF','reference_key':'lte:262-01:DEF'}, {'ts':120}]:
            d=LTEAdvance();d.feed(self.record())
            event=self.record(1,'B063',command_body(32))|change
            self.assertIsNone(d.feed(event))
            self.assertIsNone(d.value)

    def test_truncation_unknown_version_and_non_lte_reference_rejected(self):
        self.assertIsNone(rach(rach_body(10)[:-1]))
        for payload in [b'',bytes([0x33])+command_body(31)[1:],command_body(31)[:-1]]:
            with self.assertRaises(ValueError):commands(payload)
        d=LTEAdvance();d.feed(self.record())
        with self.assertRaises(ValueError):d.feed(self.record(1,key='nr:262-01:ABC'))
        self.assertIsNone(d.value)
