"""Offline tests for official CLI warning handling and dispatch guards."""
from pathlib import Path
import json,os,shutil,tempfile,unittest,zipfile,xml.etree.ElementTree as ET
import numpy as np
from slicing import cli_warnings,blocking_reasons,read_archive
from bambu_print import validate_job
from thumbnail import triangles

RISK='It seems object sample.stl has floating cantilever. Please re-orient the object or enable support generation.'

class OfficialChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def result(self,message='',log='',code=0):
        (self.root/'result.json').write_text(json.dumps({'return_code':code,'sliced_plates':[{'id':1,'warning_message':message}]}))
        (self.root/'slice.log').write_text(log)
        return cli_warnings(self.root)
    def test_clean_slice(self):
        self.assertEqual(self.result(),{'warnings':[],'support_warnings':[]})
    def test_official_risk_reasons(self):
        for reason in ['floating cantilever','floating regions','large overhangs']:
            warning=RISK.replace('floating cantilever',reason)
            self.assertEqual(self.result(warning)['support_warnings'],[warning])
    def test_log_preserves_overwritten_warnings(self):
        other='Other slicing warning'
        d=self.result(other,f'[warning] plate 1: found NON_CRITICAL slicing warnings: {RISK}\n')
        self.assertEqual(d['support_warnings'],[RISK]);self.assertIn(other,d['warnings'])
    def test_failed_slice_is_not_clear(self):
        with self.assertRaises(ValueError):self.result(code=158)
    def test_missing_warning_field_is_not_clear(self):
        (self.root/'result.json').write_text('{"return_code":0,"sliced_plates":[{"id":1}]}')
        with self.assertRaises(ValueError):cli_warnings(self.root)
    def test_risk_requires_generated_support(self):
        pre=self.result(RISK);final=self.result()
        self.assertTrue(blocking_reasons(pre,final,{'support_enabled':False,'support_paths_present':False}))
        self.assertTrue(blocking_reasons(pre,final,{'support_enabled':True,'support_paths_present':False}))
        self.assertEqual(blocking_reasons(pre,final,{'support_enabled':True,'support_paths_present':True}),[])
    def test_unrelated_preflight_warning_not_cleared_by_support(self):
        pre=self.result('Other slicing warning');final=self.result()
        self.assertTrue(blocking_reasons(pre,final,{'support_enabled':True,'support_paths_present':True}))

class DispatchGuard(unittest.TestCase):
    # No SDK/network/print calls; use a prepared artifact from this script version.
    def setUp(self):
        fixture=os.environ.get('BAMBU_TEST_JOB')
        if not fixture:self.skipTest('Set BAMBU_TEST_JOB to a CLI-checked job directory')
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'job'
        shutil.copytree(fixture,self.root)
    def tearDown(self):
        if hasattr(self,'tmp'):self.tmp.cleanup()
    def test_verified_job(self):self.assertEqual(validate_job(self.root)['material'],'PLA')
    def test_changed_file_rejected(self):
        with (self.root/'job.gcode.3mf').open('ab') as f:f.write(b'changed')
        with self.assertRaisesRegex(ValueError,'已变更'):validate_job(self.root)
    def test_attempt_marker_rejected(self):
        (self.root/'dispatch_attempt.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'已经尝试'):validate_job(self.root)
    def test_changed_cli_evidence_rejected(self):
        (self.root/'check-no-support/result.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'记录已变更'):validate_job(self.root)
    def test_changed_placement_rejected(self):
        if not (self.root/'placed.3mf').exists():self.skipTest('Requires a placement-enabled fixture')
        with (self.root/'placed.3mf').open('ab') as f:f.write(b'changed')
        with self.assertRaisesRegex(ValueError,'摆放模型已变更'):validate_job(self.root)
    def test_all_slices_keep_frozen_placement(self):
        if not (self.root/'placed.3mf').exists():self.skipTest('Requires a placement-enabled fixture')
        placed=triangles(self.root/'placed.3mf')
        job=json.loads((self.root/'job.json').read_text())
        for name in job['support_report']['passes']:
            np.testing.assert_allclose(placed,triangles(self.root/name/'job.gcode.3mf'),atol=1e-4)
    def test_legacy_geometry_report_rejected(self):
        p=self.root/'job.json';d=json.loads(p.read_text());d['support_report']={'analysis_reliable':True}
        p.write_text(json.dumps(d))
        with self.assertRaisesRegex(ValueError,'Bambu CLI'):validate_job(self.root)

class PlacementArtifacts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'model.3mf'
    def tearDown(self):self.tmp.cleanup()
    def test_preview_composes_component_and_build_transforms(self):
        namespace='http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
        root=f'''<model xmlns="{namespace}" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">
<resources><object id="2"><components><component p:path="/3D/Objects/part.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 3"/></components></object></resources>
<build><item objectid="2" transform="0 1 0 -1 0 0 0 0 1 10 20 0"/></build></model>'''
        part=f'''<model xmlns="{namespace}"><resources><object id="1"><mesh><vertices>
<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="2" z="0"/>
</vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources></model>'''
        with zipfile.ZipFile(self.path,'w') as z:
            z.writestr('3D/3dmodel.model',root);z.writestr('3D/Objects/part.model',part)
        np.testing.assert_allclose(triangles(self.path),[[[10,20,3],[10,21,3],[8,20,3]]])
    def test_cli_metadata_quote_repair_preserves_values(self):
        xml='<config><metadata key="variants" value=""A";"B""/><metadata key="normal" value="x &amp; y"/></config>'
        with zipfile.ZipFile(self.path,'w') as z:z.writestr('Metadata/model_settings.config',xml)
        root=ET.fromstring(read_archive(self.path)['Metadata/model_settings.config'])
        self.assertEqual([m.attrib['value'] for m in root],['"A";"B"','x & y'])

if __name__=='__main__':unittest.main()
