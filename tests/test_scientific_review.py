"""Evidence review stays separate from a scientific success claim."""
import unittest

from fastapi.testclient import TestClient

from auto_lammps.web import create_app
import test_results


class ScientificReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_results.ResultsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.analysis.run_analysis()
        self.task_id = self.fixture.doc['id']
        self.report_id = self.fixture.request()['reports'][0]['id']
        self.base = '/api/tasks/' + self.task_id + '/scientific-reviews'
        self.headers = {'Origin': test_results.ORIGIN, 'X-Task-Review': '1'}

    def test_review_is_task_bound_persistent_and_never_claims_scientific_success(self):
        before = self.fixture.fixture.ledger.events(self.fixture.fixture.request_id)
        self.assertEqual(self.fixture.client.get(self.base).json()['reviews'], [])
        response = self.fixture.client.post(self.base, json={'analysis_id': self.report_id}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        review = response.json()
        self.assertEqual(review['state'], 'criteria_missing')
        self.assertEqual(review['scientific_status'], 'not_evaluated')
        self.assertEqual([check['state'] for check in review['checks']],
                         ['verified', 'verified', 'verified', 'missing'])
        self.assertEqual(review['evidence']['analysis_id'], self.report_id)
        self.assertTrue(review['evidence']['source_tables'][0]['sha256'])
        self.assertEqual(self.fixture.client.post(self.base, json={'analysis_id': self.report_id},
                         headers=self.headers).json(), review)
        self.assertEqual(self.fixture.client.get(self.base).json()['reviews'], [review])
        download = self.fixture.client.get(self.base + '/' + review['id'] + '/download')
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.json(), review)
        with TestClient(create_app(self.fixture.tasks, results_reader=self.fixture.reader),
                        base_url=test_results.ORIGIN) as reopened:
            self.assertEqual(reopened.get(self.base).json()['reviews'], [review])
        other = self.fixture.tasks.create(title='other', prompt='other', mode='research')['id']
        other_url = '/api/tasks/' + other + '/scientific-reviews'
        self.assertEqual(self.fixture.client.get(other_url).json()['reviews'], [])
        self.assertEqual(self.fixture.client.get(other_url + '/' + review['id'] + '/download').status_code, 409)
        self.assertEqual(before, self.fixture.fixture.ledger.events(self.fixture.fixture.request_id))

    def test_changed_saved_report_hides_old_evidence_and_download(self):
        review = self.fixture.client.post(self.base, json={'analysis_id': self.report_id},
                                          headers=self.headers).json()
        path = self.fixture.analysis.root / 'reports' / (self.report_id + '.json')
        path.write_bytes(path.read_bytes() + b' ')
        retained = self.fixture.client.get(self.base).json()['reviews'][0]
        self.assertEqual(retained['id'], review['id'])
        self.assertEqual(retained['state'], 'source_unavailable')
        self.assertNotIn('evidence', retained)
        self.assertEqual(self.fixture.client.get(self.base + '/' + review['id'] + '/download').status_code, 409)
        self.assertEqual(self.fixture.client.post(self.base, json={'analysis_id': self.report_id},
                         headers=self.headers).status_code, 409)

    def test_missing_report_or_unfrozen_task_cannot_create_review(self):
        self.assertEqual(self.fixture.client.post(self.base, json={'analysis_id': 'a' * 64},
                         headers=self.headers).status_code, 409)
        draft = self.fixture.tasks.create(title='draft', prompt='draft', mode='research')['id']
        response = self.fixture.client.post('/api/tasks/' + draft + '/scientific-reviews',
                                            json={'analysis_id': self.report_id}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertIn('冻结', response.json()['detail'])
        self.assertEqual(self.fixture.client.get(self.base).json()['reviews'], [])


if __name__ == '__main__':
    unittest.main()
