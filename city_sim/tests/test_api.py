import tempfile
import importlib.util
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from city_sim.api import make_app
from city_sim.geodata import CityData
from city_sim.schema import WindFieldResult

OSM='''<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="synthetic-test-fixture">
<bounds minlon="158.640" minlat="53.020" maxlon="158.646" maxlat="53.026"/>
<node id="1" lon="158.641" lat="53.021"/>
<node id="2" lon="158.643" lat="53.021"/>
<node id="3" lon="158.643" lat="53.023"/>
<node id="4" lon="158.641" lat="53.023"/>
<node id="5" lon="158.644" lat="53.024"/>
<way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/><tag k="building" v="yes"/><tag k="building:levels" v="4"/></way>
<way id="20"><nd ref="2"/><nd ref="5"/><tag k="highway" v="residential"/><tag k="name" v="Test synthetic road"/></way>
</osm>'''
SCENARIO={
 'title':'Контрольный сценарий',
 'region':{'west':158.640,'south':53.020,'east':158.646,'north':53.026},
 'wind':{'speed_mps':11,'direction_from_deg':270,'reference_height_m':10},
 'available_loose_snow':'unknown', 'weather':{'kind':'manual_scenario'}
}

class FakeSolver:
    def calculate(self, scenario):
        return WindFieldResult(model_name='fake_adapter_test',model_version='0',
            grid={'origin_xy_m':[100,200],'cell_size_m':2,'width':2,'height':2,'reference_height_m':10},
            velocity_east_mps=[1.,1.,1.,1.],source_kind='synthetic_test')

class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.osm=self.root/'pilot.osm'
        self.osm.write_text(OSM,encoding='utf-8')
        self.has_snowops = importlib.util.find_spec('snowops') is not None
        self.app=make_app(db_path=self.root/'scenario.db',building_path=self.osm,graph_path=self.osm if self.has_snowops else self.root/'missing.osm')
        self.client=TestClient(self.app)

    def tearDown(self):self.tmp.cleanup()

    def test_geometry_and_graph(self):
        buildings=self.client.get('/api/v1/city/buildings').json()
        self.assertEqual(len(buildings['features']),1)
        self.assertEqual(buildings['features'][0]['properties']['height_m'],12)
        self.assertEqual(buildings['features'][0]['properties']['height_source'],'estimated_from_levels_3m_per_level')
        roads=self.client.get('/api/v1/city/roads').json()
        if self.has_snowops:
            self.assertTrue(len(roads['features'])>=1)
            self.assertTrue(roads['metadata']['graph_version'])
        else:
            self.assertEqual(roads['metadata']['status'],'source_missing')

    def test_scenarios_persist_without_solver(self):
        r=self.client.post('/api/v1/scenarios',json=SCENARIO)
        self.assertEqual(r.status_code,201,r.text)
        data=r.json()
        run=self.client.post(f"/api/v1/scenarios/{data['scenario_id']}/runs")
        self.assertEqual(run.status_code,201)
        self.assertEqual(run.json()['status'],'solver_not_connected')
        self.assertIsNone(run.json()['result'])
        self.assertEqual(self.client.get(f"/api/v1/scenarios/{data['scenario_id']}").status_code,200)
        self.assertEqual(len(self.client.get('/api/v1/scenarios').json()),1)

    def test_solver_adapter_can_be_inserted(self):
        self.app.state.wind_solver=FakeSolver()
        sid=self.client.post('/api/v1/scenarios',json=SCENARIO).json()['scenario_id']
        run=self.client.post(f'/api/v1/scenarios/{sid}/runs').json()
        self.assertEqual(run['status'],'completed')
        self.assertEqual(run['result']['grid']['crs'],'EPSG:32657')
        self.assertEqual(len(run['result']['velocity_east_mps']),4)
        self.assertEqual(self.client.get(f"/api/v1/runs/{run['run_id']}/field").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/runs/{run['run_id']}/assets").status_code, 404)

    def test_invalid_bbox_and_grid_rejected(self):
        self.assertEqual(self.client.get('/api/v1/city/buildings?bbox=0,0,2,2').status_code,422)
        bad={**SCENARIO,'region':{'west':158,'south':53,'east':159,'north':54}}
        self.assertEqual(self.client.post('/api/v1/scenarios',json=bad).status_code,422)
        with self.assertRaises(ValueError):
            WindFieldResult(model_name='x',model_version='1',grid={'origin_xy_m':[0,0],
              'width':2,'height':2,'cell_size_m':1,'reference_height_m':10},
              pressure_pa=[1,2],source_kind='synthetic_test')

    def test_data_revision_cannot_silently_change(self):
        sid=self.client.post('/api/v1/scenarios',json=SCENARIO).json()['scenario_id']
        self.osm.write_text(OSM+'\n<!-- updated -->',encoding='utf-8')
        r=self.client.post(f'/api/v1/scenarios/{sid}/runs')
        self.assertEqual(r.status_code,409)

if __name__=='__main__':unittest.main()
