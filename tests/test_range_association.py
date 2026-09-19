import unittest
from drive_app import Survey
from nr_localization import associate_range, candidate_areas, DiagnosticPacket
from qmi_location import associate_snapshot

class RangeAssociationTests(unittest.TestCase):
    def setUp(self):
        self.radio=dict(ts=100,subscription=2,session_id='s',association_epoch='e',
                        nr_band='n78',nr_pci=12,nr_arfcn=640000,nr_plmn='262-02',nr_cell_id='ABC')
        self.row=dict(ts=101,subscription=2,rat='nr',band='n78',pci=12,channel=640000,
                      range_m=820,step_m=39,acc_m=5,lat=48,lon=11,reason='Experimental')
    def test_scope_matches_at_event_and_does_not_cross_sim_or_handover(self):
        radios=[self.radio,dict(self.radio,ts=102)]
        accepted=associate_range(self.row,radios,'s','e')
        self.assertTrue(accepted['map_eligible'])
        self.assertEqual(accepted['plmn'],'262-02')
        for changed in [dict(subscription=1),dict(nr_cell_id='DEF'),dict(association_epoch='new'),dict(nr_arfcn=645000)]:
            self.assertFalse(associate_range(self.row,[radios[0],dict(radios[1],**changed)],'s','e')['map_eligible'])
        self.assertFalse(associate_range(dict(self.row,subscription=None),radios,'s','e')['map_eligible'])
        self.assertFalse(associate_range(self.row,[radios[0]],'s','e')['map_eligible'])
    def test_unscoped_observations_never_form_area(self):
        self.assertEqual(candidate_areas([self.row]*5,'s','e'),[])
    def test_qmi_snapshot_retains_unknown_age_and_no_position(self):
        row=dict(subscription=2,plmn='262-02',cell_id='ABC',pci=12,channel=1801,
                 received_ts=101,measurement_ts=None,range_m=2000)
        radio=dict(subscription=2,lte_plmn='262-02',lte_cell_id='ABC',lte_pci=12.0,lte_arfcn=1801.0,lte_band='B3')
        out=associate_snapshot(row,radio,radio,'s','e')
        self.assertFalse(out['map_eligible'])
        self.assertIsNone(out['lat'])
        with self.assertRaises(ValueError):associate_snapshot(row,radio,dict(radio,subscription=1),'s','e')
    def test_epoch_rotates_on_subscription_or_cell_change_not_signal(self):
        survey=Survey(':memory:')
        try:
            r=dict(rat='LTE',subscription=2,lte_band='B3',lte_plmn='262-02',lte_cell_id='ABC',lte_pci=12,lte_arfcn=1801)
            survey.record('radio',r);epoch=survey.association_epoch
            survey.record('radio',dict(r,lte_rsrp=-99));self.assertEqual(epoch,survey.association_epoch)
            survey.record('radio',dict(r,subscription=1));self.assertNotEqual(epoch,survey.association_epoch)
        finally:survey.db.close()
