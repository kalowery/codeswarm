import time
import unittest

from router.providers.factory import build_providers
from router import router as router_module


class _SlowProvider:
    def __init__(self, delay_s):
        self.delay_s = delay_s

    def list_active_jobs(self):
        time.sleep(self.delay_s)
        return {}


class _HealthyProvider:
    def list_active_jobs(self):
        return {}


class _FailingProvider:
    def __init__(self, message):
        self.message = message

    def list_active_jobs(self):
        raise RuntimeError(self.message)


class ProviderStatusTests(unittest.TestCase):
    def test_build_providers_propagates_duplicate_provider_ref_status(self):
        providers, specs = build_providers(
            {},
            [
                {"id": "bad-a", "backend": "invalid", "provider_ref": "invalid:shared"},
                {"id": "bad-b", "backend": "invalid", "provider_ref": "invalid:shared"},
            ],
        )
        self.assertEqual(providers, {})
        self.assertEqual(len(specs), 2)
        self.assertTrue(all(spec.get("disabled") for spec in specs))
        self.assertTrue(all("Unsupported cluster backend" in str(spec.get("disabled_reason")) for spec in specs))

    def test_reconcile_marks_timeout_provider_disabled(self):
        original_specs = router_module.PROVIDER_SPECS
        original_swarms = router_module.SWARMS
        original_job_to_swarm = router_module.JOB_TO_SWARM
        try:
            router_module.PROVIDER_SPECS = [
                {
                    "id": "slow",
                    "backend": "slurm",
                    "provider_ref": "slurm:default",
                    "disabled": False,
                    "disabled_reason": None,
                }
            ]
            router_module.SWARMS = {}
            router_module.JOB_TO_SWARM = {}
            router_module.reconcile(
                {"slurm:default": _SlowProvider(0.05)},
                {"router": {"startup_reconcile_timeout_seconds": 0.01}},
            )
            spec = router_module.PROVIDER_SPECS[0]
            self.assertTrue(spec.get("disabled"))
            self.assertIn("timed out", str(spec.get("disabled_reason")).lower())
        finally:
            router_module.PROVIDER_SPECS = original_specs
            router_module.SWARMS = original_swarms
            router_module.JOB_TO_SWARM = original_job_to_swarm

    def test_reconcile_clears_disabled_on_success(self):
        original_specs = router_module.PROVIDER_SPECS
        original_swarms = router_module.SWARMS
        original_job_to_swarm = router_module.JOB_TO_SWARM
        try:
            router_module.PROVIDER_SPECS = [
                {
                    "id": "aws-default",
                    "backend": "aws",
                    "provider_ref": "aws:default",
                    "disabled": True,
                    "disabled_reason": "previous failure",
                }
            ]
            router_module.SWARMS = {}
            router_module.JOB_TO_SWARM = {}
            router_module.reconcile(
                {"aws:default": _HealthyProvider()},
                {"router": {"startup_reconcile_timeout_seconds": 1}},
            )
            spec = router_module.PROVIDER_SPECS[0]
            self.assertFalse(spec.get("disabled"))
            self.assertIsNone(spec.get("disabled_reason"))
        finally:
            router_module.PROVIDER_SPECS = original_specs
            router_module.SWARMS = original_swarms
            router_module.JOB_TO_SWARM = original_job_to_swarm

    def test_reconcile_marks_failure_provider_disabled(self):
        original_specs = router_module.PROVIDER_SPECS
        original_swarms = router_module.SWARMS
        original_job_to_swarm = router_module.JOB_TO_SWARM
        try:
            router_module.PROVIDER_SPECS = [
                {
                    "id": "aws-default",
                    "backend": "aws",
                    "provider_ref": "aws:default",
                    "disabled": False,
                    "disabled_reason": None,
                }
            ]
            router_module.SWARMS = {}
            router_module.JOB_TO_SWARM = {}
            router_module.reconcile(
                {"aws:default": _FailingProvider("boom")},
                {"router": {"startup_reconcile_timeout_seconds": 1}},
            )
            spec = router_module.PROVIDER_SPECS[0]
            self.assertTrue(spec.get("disabled"))
            self.assertIn("boom", str(spec.get("disabled_reason")))
        finally:
            router_module.PROVIDER_SPECS = original_specs
            router_module.SWARMS = original_swarms
            router_module.JOB_TO_SWARM = original_job_to_swarm


if __name__ == "__main__":
    unittest.main()
