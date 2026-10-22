import unittest
from unittest.mock import patch

from services import runtime_environment_service as module


class RuntimeEnvironmentServiceTest(unittest.TestCase):
    def setUp(self):
        module._RUNTIME_SNAPSHOT_CACHE = None
        module._NETWORK_SAMPLE = None

    def test_snapshot_shape_and_cache(self):
        first = module.snapshot()
        expected = {
            "runtime_mode", "instance_name", "distribution", "kernel_version", "architecture",
            "python_version", "cpu_capacity", "service_started_at", "service_uptime_seconds",
            "process_cpu_percent", "process_memory_bytes", "process_memory_percent", "memory_scope",
            "memory_percent", "storage_percent", "network_rx_bytes_per_sec", "network_tx_bytes_per_sec",
        }
        self.assertEqual(set(first), expected)
        self.assertIn(first["runtime_mode"], {"docker", "native"})
        with patch.object(module, "_capture_snapshot", side_effect=AssertionError("must hit cache")):
            self.assertEqual(module.snapshot(), first)

    def test_network_rate_needs_two_samples(self):
        with patch.object(module, "_read_network_bytes", side_effect=[(1000, 500), (3000, 1500)]), \
                patch.object(module.time, "monotonic", side_effect=[10.0, 12.0]):
            self.assertEqual(module._network_rates(), (None, None))
            self.assertEqual(module._network_rates(), (1000.0, 500.0))

    def test_percentage_helpers(self):
        self.assertEqual(module._percentage(50, 200), 25.0)
        self.assertIsNone(module._percentage(None, 200))
        self.assertIsNone(module._percentage(1, 0))


if __name__ == "__main__":
    unittest.main()
