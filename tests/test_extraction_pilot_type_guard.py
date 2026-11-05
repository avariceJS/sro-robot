import unittest
from scripts.dev.extraction_pilot.type_guard import guard
class AddressTypeGuardTests(unittest.TestCase):
    def row(self,label):
        text=label+': 123456, область Примерная, г. Тест, ул. Первая, д. 1'
        return {'pages':[{'text':text}],'review':[{'attr':'address','source_start':text.index('г. Тест')},{'attr':'address_kind','value':'actual'}]}
    def test_generic_label_withholds_actual_without_changing_address(self):
        row=self.row('адрес');self.assertEqual(len(guard(row)),1);self.assertEqual(row['review'][1]['value'],'')
    def test_explicit_residence_label_retains_type(self):
        row=self.row('адрес проживания');self.assertEqual(guard(row),[]);self.assertEqual(row['review'][1]['value'],'actual')
if __name__=='__main__':unittest.main()
