"""Synthetic checks of isolated pilot provenance and coordinate boundaries."""
import unittest
from scripts.dev.extraction_pilot.harness import map_box, text_lines, checked_source, formal, normalization_support
class PilotEvidenceTests(unittest.TestCase):
    def test_split_rotation_returns_to_original_with_crop_offset(self):
        self.assertEqual(map_box([10,20,100,40],[[0,-1,3024],[1,0,1976],[0,0,1]]),[2964,1986,40,100])
    def test_repeated_value_uses_cited_occurrence(self):
        text='other 123\nowner 123'
        page={'page_id':'p1','text':text,'lines':text_lines(text,'p1'),'words':[{'start':6,'end':9,'tag':'other'},{'start':16,'end':19,'tag':'owner'}]}
        evidence=checked_source({'raw':'123','src':'p1:L0001'},page,'C')
        self.assertEqual([w['tag'] for w in evidence['boxes']],['owner'])
    def test_false_source_and_quote_do_not_get_boxes(self):
        page={'page_id':'p','text':'alpha beta','lines':text_lines('alpha beta','p'),'words':[]}
        self.assertEqual(checked_source({'raw':'alpha','src':'p:L0099'},page,'C')['status'],'invalid_source_id')
        self.assertEqual(checked_source({'raw':'gamma','src':'p:L0000'},page,'C')['status'],'quote_mismatch')
    def test_extra_invalid_id_cannot_hide_behind_a_valid_id(self):
        page={'page_id':'p','text':'alpha','lines':text_lines('alpha','p'),'words':[]}
        self.assertEqual(checked_source({'raw':'alpha','src':'p:L0000,p:L9999'},page,'C')['status'],'invalid_source_id')
    def test_visual_quote_does_not_acquire_precise_boxes(self):
        e=checked_source({'raw':'alpha','src':'p'},{'page_id':'p','text':'alpha','words':[]},'D')
        self.assertEqual(e['granularity'],'page');self.assertEqual(e['boxes'],[])
    def test_invented_postcode_and_generic_address_type_are_flagged(self):
        self.assertEqual(normalization_support('address','ул. Тестовая, д. 1','119048, ул. Тестовая, д. 1'),'unsupported_address_change')
        self.assertEqual(normalization_support('address_kind','адрес','actual'),'unsupported_address_type')
        self.assertEqual(normalization_support('address_kind','адрес проживания','actual'),'supported')
    def test_identifier_invalidity_is_preserved(self):
        self.assertEqual(formal('inn','123'),'invalid_checksum_or_format')
if __name__=='__main__':unittest.main()
