"""Offline acceptance for the supported runtime, independent of old backends."""
import unittest


def run_checks(services_only=False):
    modules = ['audit.test_single_stack', 'audit.test_inner_life', 'audit.test_task_consent',
               'audit.test_recollection_solver', 'audit.test_readable_logs', 'audit.test_thought_timing',
               'audit.test_startup_speech','audit.test_solver_completion']
    if not services_only:
        modules += ['audit.test_conversation_control','audit.test_action_notice','audit.test_message_layout',
                    'audit.test_runtime_delivery','audit.test_release_controller','audit.test_local_plugin']
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in modules)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1
