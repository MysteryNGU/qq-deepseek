import os
import unittest
from unittest.mock import patch

from life.process_access import process_integrity,worker_access


class ProcessAccessTests(unittest.TestCase):
    def test_low_worker_warning_does_not_mislabel_requested_dsh_policy(self):
        with patch('life.process_access.process_integrity',side_effect=[
                dict(status='observed',rid=8192,level='medium'),
                dict(status='observed',rid=4096,level='low')]):
            result = worker_access(123)
        self.assertEqual(result['file_policy_requested'],'danger-full-access')
        self.assertIn('Windows',result['warning'])
        self.assertEqual(result['worker']['level'],'low')

    def test_missing_observation_is_unavailable_not_proof_of_writable_or_readonly(self):
        with patch('life.process_access.process_integrity',return_value=dict(status='unavailable')):
            self.assertNotIn('warning',worker_access(123))

    @unittest.skipUnless(os.name=='nt','Windows process tokens')
    def test_real_current_process_token_and_missing_process(self):
        result = process_integrity(os.getpid())
        self.assertEqual(result['status'],'observed')
        self.assertIsInstance(result['rid'],int)
        self.assertEqual(process_integrity(0)['status'],'unavailable')
