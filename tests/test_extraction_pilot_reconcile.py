import unittest
from scripts.dev.extraction_pilot.reconcile import reconcile
class OccurrenceTests(unittest.TestCase):
    def test_same_name_is_candidate_only_and_incomplete_does_not_imply_absence(self):
        party={'name':{'value':'Иванов Иван Иванович'},'roles':[{'role':'manager','status':'current'}],'fields':[]}
        r=reconcile({'source_id':'synthetic','route':'C','calls':[{'page_id':'p1','result':{'participants':[party]},'incomplete':False},{'page_id':'p2','result':{'participants':[party]},'incomplete':True}]})
        self.assertEqual(len(r['participants']),2);self.assertFalse(r['candidate_identity_links'][0]['confirmed_identity']);self.assertFalse(r['absence_inference_permitted']);self.assertFalse(r['gold_used']);self.assertEqual(r['coverage'][1]['state'],'incomplete')

    def test_nested_retry_keeps_leaf_alternatives_and_lineage(self):
        def result(date,person):
            return {'document':{'doc_date':{'raw':date,'value':date,'src':'p1:L0000'}},'participants':[{'name':{'value':person},'roles':[],'fields':[]}]}
        row={'source_id':'synthetic','route':'C','calls':[{
            'page_id':'p1','result':result('01.01.2000','Discarded Parent'),
            'initial_attempt':{'finish_reason':'length'},'incomplete':False,
            'retry_calls':[
                {'page_id':'p1','result':result('02.01.2000','Leaf One'),'incomplete':False},
                {'page_id':'p1','result':result('03.01.2000','Discarded Nested'),
                 'initial_attempt':{'finish_reason':'length'},'incomplete':False,
                 'retry_calls':[{'page_id':'p1','result':result('03.01.2000','Leaf Two'),'incomplete':False}]},
            ],
        }]}
        projection=reconcile(row)
        self.assertEqual([x['call_path'] for x in projection['document_candidates']['doc_date']],[[0,0],[0,1,0]])
        self.assertEqual([x['value'] for x in projection['document_candidates']['doc_date']],['02.01.2000','03.01.2000'])
        self.assertEqual([x['party']['name']['value'] for x in projection['participants']],['Leaf One','Leaf Two'])
        self.assertEqual([x['call_path'] for x in projection['discarded_initial_attempts']],[[0],[0,1]])
        self.assertEqual(projection['conflicts'][0]['kind'],'document_field_conflict')
        self.assertFalse(projection['incomplete_coverage'])

    def test_failed_retry_leaf_keeps_incomplete_coverage_without_absence_claim(self):
        row={'source_id':'synthetic','route':'D','calls':[{
            'page_id':'p1','result':{'document':{},'participants':[]},
            'initial_attempt':{'error':'context_admission_exceeded'},'incomplete':True,
            'retry_calls':[
                {'page_id':'p1','result':{'document':{},'participants':[]},'incomplete':False},
                {'page_id':'p1','error':'TimeoutError','incomplete':True},
            ],
        }]}
        projection=reconcile(row)
        self.assertEqual([x['state'] for x in projection['coverage']],['processed_provided_fragment','incomplete'])
        self.assertEqual(projection['coverage'][1]['call_path'],[0,1])
        self.assertTrue(projection['incomplete_coverage'])
        self.assertFalse(projection['absence_inference_permitted'])
        self.assertEqual(projection['document_candidates'],{})
if __name__=='__main__':unittest.main()
