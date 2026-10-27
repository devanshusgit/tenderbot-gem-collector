import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import urllib.error

from source_health import check_source, record_failure, may_attempt_browser


class SourceHealthTests(unittest.TestCase):
    def test_connection_refusal_is_transport_failure(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(ConnectionRefusedError(111, 'refused'))
        result = check_source(opener)
        self.assertEqual(result['stage'], 'connect')
        self.assertEqual(result['errno'], 111)
        self.assertEqual(result['status'], 'unavailable')

    def test_source_500_is_distinguished_from_connection_failure(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://bidplus.gem.gov.in/all-bids', 500, 'error', {}, None)
        result = check_source(opener)
        self.assertEqual(result['http_status'], 500)
        self.assertTrue(may_attempt_browser(result))
        self.assertFalse(may_attempt_browser({'stage': 'connect', 'status': 'unavailable'}))
        self.assertFalse(may_attempt_browser({'stage': 'http', 'status': 'blocked'}))

    def test_failure_keeps_records_freshness_success_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder/'state.json').write_text(json.dumps({'page': 88, 'complete': False, 'last_success_at': 'previous'}))
            (folder/'collector.json').write_text(json.dumps({'records': 18236, 'fetched_at': 'original', 'documents': 30}))
            result = {'status': 'unavailable', 'stage': 'connect', 'errno': 111}
            record_failure(folder, result)
            state = json.loads((folder/'state.json').read_text())
            status = json.loads((folder/'collector.json').read_text())
            self.assertEqual(state['page'], 88)
            self.assertEqual(state['last_success_at'], 'previous')
            self.assertEqual(status['records'], 18236)
            self.assertEqual(status['fetched_at'], 'original')
            self.assertEqual(status['documents'], 30)
            self.assertEqual(status['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
