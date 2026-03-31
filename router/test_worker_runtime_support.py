import tempfile
import unittest
from unittest.mock import patch

from agent import claude_worker as claude_worker_module
from router import router as router_module
from router.providers.factory import _default_launch_fields_for_backend, get_provider_specs
from router.providers.local import LocalProvider


class WorkerRuntimeSupportTests(unittest.TestCase):
    def test_local_launch_fields_include_claude_runtime(self):
        fields = _default_launch_fields_for_backend("local", {})
        worker_mode_field = next(
            (field for field in fields if field.get("key") == "worker_mode"),
            None,
        )
        self.assertIsNotNone(worker_mode_field)
        options = worker_mode_field.get("options") or []
        values = {option.get("value") for option in options if isinstance(option, dict)}
        self.assertIn("claude", values)

    def test_local_provider_allows_claude_with_interactive_approval_policy(self):
        captured = {}

        class _DummyProc:
            pid = 43211

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = LocalProvider({"workspace_root": temp_dir})
            def fake_popen(cmd, cwd=None, env=None):
                captured["env"] = dict(env or {})
                return _DummyProc()
            with patch("router.providers.local.importlib.util.find_spec", return_value=object()):
                with patch("router.providers.local.subprocess.Popen", side_effect=fake_popen):
                    with patch.object(provider, "_read_proc_start_ticks", return_value=123):
                        provider.launch(
                            1,
                            launch_params={
                                "worker_mode": "claude",
                                "approval_policy": "on-request",
                            },
                        )
        self.assertEqual(captured["env"].get("CODESWARM_CLAUDE_PERMISSION_MODE"), "default")

    def test_local_provider_rejects_claude_when_sdk_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = LocalProvider({"workspace_root": temp_dir})
            with patch("router.providers.local.importlib.util.find_spec", return_value=None):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "claude-agent-sdk",
                ):
                    provider.launch(
                        1,
                        launch_params={
                            "worker_mode": "claude",
                            "approval_policy": "never",
                        },
                    )

    def test_local_launch_fields_include_claude_env_profile_options(self):
        fields = _default_launch_fields_for_backend(
            "local",
            {
                "claude_env_profiles": {
                    "amd-llm-gateway": {
                        "ANTHROPIC_BASE_URL": "https://llm-api.amd.com/Anthropic",
                    }
                }
            },
        )
        profile_field = next(
            (field for field in fields if field.get("key") == "claude_env_profile"),
            None,
        )
        self.assertIsNotNone(profile_field)
        options = profile_field.get("options") or []
        self.assertIn(
            "amd-llm-gateway",
            {option.get("value") for option in options if isinstance(option, dict)},
        )

    def test_flat_local_cluster_config_exposes_claude_env_profiles(self):
        specs = get_provider_specs(
            {
                "cluster": {
                    "backend": "local",
                    "workspace_root": "runs",
                    "claude_env_profiles": {
                        "amd-llm-gateway": {
                            "ANTHROPIC_BASE_URL": "https://llm-api.amd.com/Anthropic",
                        }
                    },
                },
                "launch_providers": [
                    {
                        "id": "local-dev",
                        "backend": "local",
                        "defaults": {
                            "claude_env_profile": "amd-llm-gateway",
                        },
                    }
                ],
            }
        )
        fields = specs[0].get("launch_fields") or []
        self.assertTrue(
            any(field.get("key") == "claude_env_profile" for field in fields if isinstance(field, dict))
        )

    def test_local_provider_applies_claude_env_profile_with_expansion(self):
        captured = {}

        class _DummyProc:
            pid = 43210

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = LocalProvider(
                {
                    "workspace_root": temp_dir,
                    "claude_env_profiles": {
                        "amd-llm-gateway": {
                            "ANTHROPIC_API_KEY": "dummy",
                            "ANTHROPIC_BASE_URL": "https://llm-api.amd.com/Anthropic",
                            "ANTHROPIC_CUSTOM_HEADERS": "Ocp-Apim-Subscription-Key: ${LLM_GATEWAY_KEY}",
                            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                        }
                    },
                }
            )

            def fake_popen(cmd, cwd=None, env=None):
                captured["cmd"] = cmd
                captured["cwd"] = cwd
                captured["env"] = dict(env or {})
                return _DummyProc()

            with patch("router.providers.local.importlib.util.find_spec", return_value=object()):
                with patch("router.providers.local.subprocess.Popen", side_effect=fake_popen):
                    with patch.object(provider, "_read_proc_start_ticks", return_value=123):
                        with patch.dict("os.environ", {"LLM_GATEWAY_KEY": "gateway-secret"}, clear=False):
                            provider.launch(
                                1,
                                launch_params={
                                    "worker_mode": "claude",
                                    "approval_policy": "never",
                                    "claude_env_profile": "amd-llm-gateway",
                                },
                            )

        env = captured["env"]
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "dummy")
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"), "https://llm-api.amd.com/Anthropic")
        self.assertEqual(
            env.get("ANTHROPIC_CUSTOM_HEADERS"),
            "Ocp-Apim-Subscription-Key: gateway-secret",
        )
        self.assertEqual(env.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"), "1")

    def test_local_provider_reports_starting_during_startup_grace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = LocalProvider({"workspace_root": temp_dir, "worker_startup_grace_seconds": 10})
            provider.jobs["local_test"] = [{"pid": 1234, "node_id": 0, "start_ticks": None}]
            provider._write_job_metadata(
                "local_test",
                {
                    "job_id": "local_test",
                    "workers": [{"pid": 1234, "node_id": 0, "start_ticks": None}],
                    "launched_at": __import__("time").time(),
                },
            )
            with patch.object(provider, "_active_workers_for_job", return_value=[]):
                self.assertEqual(provider.get_job_state("local_test"), "STARTING")

    def test_local_provider_rejects_claude_env_profile_when_placeholder_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = LocalProvider(
                {
                    "workspace_root": temp_dir,
                    "claude_env_profiles": {
                        "amd-llm-gateway": {
                            "ANTHROPIC_CUSTOM_HEADERS": "Ocp-Apim-Subscription-Key: ${LLM_GATEWAY_KEY}",
                        }
                    },
                }
            )
            with patch("router.providers.local.importlib.util.find_spec", return_value=object()):
                with self.assertRaisesRegex(RuntimeError, "LLM_GATEWAY_KEY"):
                    with patch.dict("os.environ", {}, clear=True):
                        provider.launch(
                            1,
                            launch_params={
                                "worker_mode": "claude",
                                "approval_policy": "never",
                                "claude_env_profile": "amd-llm-gateway",
                            },
                        )

    def test_translate_event_accepts_canonical_worker_event(self):
        original_job_to_swarm = router_module.JOB_TO_SWARM
        try:
            router_module.JOB_TO_SWARM = {"job-1": "swarm-1"}
            translated = router_module.translate_event(
                {
                    "type": "worker_event",
                    "job_id": "job-1",
                    "node_id": 0,
                    "injection_id": "inj-1",
                    "event": "assistant",
                    "payload": {
                        "content": "hello",
                        "final_answer": True,
                    },
                }
            )
            self.assertEqual(
                translated,
                (
                    "assistant",
                    {
                        "swarm_id": "swarm-1",
                        "job_id": "job-1",
                        "node_id": 0,
                        "injection_id": "inj-1",
                        "content": "hello",
                        "final_answer": True,
                        "raw": {
                            "type": "worker_event",
                            "job_id": "job-1",
                            "node_id": 0,
                            "injection_id": "inj-1",
                            "event": "assistant",
                            "payload": {
                                "content": "hello",
                                "final_answer": True,
                            },
                        },
                    },
                ),
            )
        finally:
            router_module.JOB_TO_SWARM = original_job_to_swarm

    def test_register_canonical_exec_approval_tracks_pending_item(self):
        original_pending = router_module.PENDING_APPROVALS
        original_version = router_module.APPROVALS_VERSION
        try:
            router_module.PENDING_APPROVALS = {}
            router_module.APPROVALS_VERSION = 0
            approval_id = router_module._register_canonical_exec_approval(
                {
                    "swarm_id": "swarm-1",
                    "job_id": "job-1",
                    "node_id": 0,
                    "injection_id": "inj-1",
                    "call_id": "call-1",
                    "command": "pwd",
                    "reason": "Approve Claude Bash command",
                    "cwd": "/tmp",
                    "approval_method": "claude/can_use_tool",
                    "available_decisions": ["accept", "cancel"],
                }
            )
            self.assertIsInstance(approval_id, str)
            meta = router_module.PENDING_APPROVALS[("job-1", 0, "call-1")]
            self.assertEqual(meta["approval_method"], "claude/can_use_tool")
            self.assertEqual(meta["available_decisions"], ["accept", "cancel"])
        finally:
            router_module.PENDING_APPROVALS = original_pending
            router_module.APPROVALS_VERSION = original_version

    def test_launch_only_injects_nonempty_system_prompt(self):
        self.assertFalse(router_module._has_nonempty_text(""))
        self.assertFalse(router_module._has_nonempty_text("   "))
        self.assertFalse(router_module._has_nonempty_text(None))
        self.assertTrue(router_module._has_nonempty_text("hello"))

    def test_claude_usage_helper_skips_zero_only_payloads(self):
        self.assertFalse(
            claude_worker_module._usage_has_nonzero_values(
                {
                    "total_tokens": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                }
            )
        )
        self.assertTrue(
            claude_worker_module._usage_has_nonzero_values(
                {
                    "total_tokens": 1,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                }
            )
        )

    def test_claude_decision_helper_accepts_notification_style_decisions(self):
        self.assertTrue(claude_worker_module._decision_is_approved("accept", None))
        self.assertFalse(claude_worker_module._decision_is_approved("cancel", None))
        self.assertTrue(
            claude_worker_module._decision_is_approved(
                {"acceptWithExecpolicyAmendment": {"proposed_execpolicy_amendment": ["git", "status"]}},
                None,
            )
        )


if __name__ == "__main__":
    unittest.main()
