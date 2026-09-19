import copy
import struct
import unittest
from nr_rrc import configuration, decode


def group():
    return {'cellGroupId': 0, 'spCellConfig': {
        'reconfigurationWithSync': {'newUE-Identity': 123, 'spCellConfigCommon': {
            'physCellId': 414,
            'downlinkConfigCommon': {'frequencyInfoDL': {'absoluteFrequencySSB': 641760, 'frequencyBandList': [78]}},
            'uplinkConfigCommon': {'initialUplinkBWP': {'genericParameters': {'subcarrierSpacing': 'kHz30'}}}}},
        'spCellConfigDedicated': {'tag-Id': 0, 'uplinkConfig': {'firstActiveUplinkBWP-Id': 1,
            'uplinkBWP-ToAddModList': [{'bwp-Id': 1, 'bwp-Common': {'genericParameters': {'subcarrierSpacing': 'kHz60'}}}]}}},
        'mac-CellGroupConfig': {'tag-Config': {'tag-ToAddModList': [{'tag-Id': 0, 'timeAlignmentTimer': 'ms500'}]}}}


class RrcTests(unittest.TestCase):
    def test_scoped_numerologies_and_timer(self):
        result = configuration(group())
        self.assertEqual((result['mu'], result['active_mu']), (1, 2))
        self.assertEqual(result['alignment_timer'], 'ms500')
        self.assertEqual(result['tag_id'], 0)

    def test_no_guess_for_missing_context(self):
        value = group()
        del value['spCellConfig']['reconfigurationWithSync']
        self.assertIsNone(configuration(value))
        value = group()
        value['spCellConfig']['spCellConfigDedicated']['uplinkConfig']['firstActiveUplinkBWP-Id'] = 2
        self.assertIsNone(configuration(value)['active_mu'])

    def test_wire_setup_without_cell_context_rejected(self):
        try:
            from pycrate_asn1dir import RRCNR
        except ImportError:
            self.skipTest('pycrate optional')
        defs = RRCNR.NR_RRC_Definitions
        defs.CellGroupConfig.set_val({'cellGroupId': 0})
        encoded = defs.CellGroupConfig.to_uper()
        defs.DL_CCCH_Message.set_val({'message': ('c1', ('rrcSetup', {
            'rrc-TransactionIdentifier': 0, 'criticalExtensions': ('rrcSetup', {
                'radioBearerConfig': {}, 'masterCellGroup': encoded})}))})
        payload = defs.DL_CCCH_Message.to_uper()
        head = struct.pack('<I', 26) + struct.pack('<BBBH Q I3sBIHBBBB',
            17, 0, 1, 414, 0, 641760, b'\0'*3, 3, 0, len(payload), 0, 0, 0, 0)
        self.assertEqual(decode(head + payload, 1), [])
        self.assertEqual(decode(head + payload[:-1], 1), [])

    def test_sa_setup_with_complete_group_roundtrip(self):
        try:
            from pycrate_asn1dir import RRCNR
        except ImportError:
            self.skipTest('pycrate optional')
        defs = RRCNR.NR_RRC_Definitions
        defs.DL_CCCH_Message.set_val({'message': ('c1', ('rrcSetup', {
            'rrc-TransactionIdentifier': 0, 'criticalExtensions': ('rrcSetup', {
                'radioBearerConfig': {}, 'masterCellGroup': SA_GROUP})}))})
        payload = defs.DL_CCCH_Message.to_uper()
        head = struct.pack('<I', 26) + struct.pack('<BBBH Q I3sBIHBBBB',
            17, 0, 1, 409, 0, 154570, b'\0'*3, 3, 0, len(payload), 0, 0, 0, 0)
        result = decode(head + payload, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]['pci'], result[0]['mu'], result[0]['group_scope']),
                         (409, 0, 'masterCellGroup'))

    def test_sa_dcch_reconfiguration_master_group(self):
        try:
            from pycrate_asn1dir import RRCNR
        except ImportError:
            self.skipTest('pycrate optional')
        defs = RRCNR.NR_RRC_Definitions
        defs.DL_DCCH_Message.set_val({'message': ('c1', ('rrcReconfiguration', {
            'rrc-TransactionIdentifier': 1, 'criticalExtensions': ('rrcReconfiguration', {
                'nonCriticalExtension': {'masterCellGroup': SA_GROUP}})}))})
        payload = defs.DL_DCCH_Message.to_uper()
        head = struct.pack('<I', 26) + struct.pack('<BBBH Q I3sBIHBBBB',
            17, 0, 1, 409, 0, 154570, b'\0'*3, 4, 0, len(payload), 0, 0, 0, 0)
        result = decode(head + payload, 2)
        self.assertEqual((result[0]['rrc_pdu'], result[0]['group_scope']), (4, 'masterCellGroup'))
        self.assertEqual(result[0]['alignment_timer'], 'infinity')

# Captured valid CellGroupConfig, reused inside synthetic SA wrappers.
SA_GROUP = bytes.fromhex('5e00b04433c55fca120dc4e81fa78d5514a9780422445ca0f36c041add3a771332d096f2803612c200000019b1b648ac5a0dc1008000004200694028806b010b60036116a40000019b8db2458024013171a88332a1cbce08dc21b8637106fb64d6655ba22898b15d76e0040480326119636c918f9b84010000008400041041200500314a01440358084da8048ff800000000080020b125b86210004488bb88208002448bb8a204001448b290706510a5f0200260c01240012140d4841016184954001c50c00ca0008123865c6d922c0120098b8d4419950e5e7046e10dc31b8837db26b32acd555210700041030814307211024560000332140702001992a0382198008500c18cc004a806106600294030a330016a018619800c500c38cc006a8067c00332d40710031830a0c48c006c282058030103fcd159ddde697d24d002003053b38237086e18dc41b31ffa147398a4e5028200805700008000100000000030ca82803115515811c00042100038859970c038080882000710b32e18070201084000e21665c400e0602208001c42ccb8800c10c132a000e2199508104400c0004080c0100100e0150007142000000400800300a004c05000c09780020b31041043c27fc7e06636f028040f31041043c2ffe57fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffe1e6350404000004080014000081000000cece3679a4e57001349c6d600002f46225f35061558051018202ac03580c901216010854074012a218308c9112f9ab00a20304055806b019202432a0123fffffffffe0200082c70ee18840011222ee2082000d122ca41c19470948080098100490005450602a8d4c848092b0c146a0070398b4cf6aa0070398c0ff88a0070490627fc850038248323fe22801c124599ff21400e0922d0ff8aa00704906a7fc950038248363fe2a801c1245b9ff25400e092304b1b08ca74adae72a0a00cc55440047000108400702067a48007010110400702067a48007020108400702067a48807030110400702067a488030573fab2801c0e679c202088018000810180400281c00600703980000004008003012008c07000c020')


if __name__ == '__main__':
    unittest.main()
