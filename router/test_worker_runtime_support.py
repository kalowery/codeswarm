import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch
from pathlib import Path

from agent import claude_worker as claude_worker_module
from router import router as router_module
from router.providers import local as local_module
from router.providers.factory import _default_launch_fields_for_backend, get_provider_specs
from router.providers.aws import AwsProvider
from router.providers.local import LocalProvider
from router.providers.slurm import SlurmProvider
from slurm import allocate_and_prepare as slurm_allocate_module


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
        execution_mode_field = next((field for field in fields if field.get("key") == "execution_mode"), None)
        self.assertIsNotNone(execution_mode_field)
        self.assertIn(
            "container",
            {option.get("value") for option in (execution_mode_field.get("options") or []) if isinstance(option, dict)},
        )
        container_engine_field = next((field for field in fields if field.get("key") == "container_engine"), None)
        self.assertIsNotNone(container_engine_field)
        self.assertIn(
            "docker",
            {option.get("value") for option in (container_engine_field.get("options") or []) if isinstance(option, dict)},
        )

    def test_aws_launch_fields_include_claude_runtime(self):
        fields = _default_launch_fields_for_backend("aws", {})
        worker_mode_field = next(
            (field for field in fields if field.get("key") == "worker_mode"),
            None,
        )
        self.assertIsNotNone(worker_mode_field)
        options = worker_mode_field.get("options") or []
        values = {option.get("value") for option in options if isinstance(option, dict)}
        self.assertIn("claude", values)
        field_keys = [field.get("key") for field in fields if isinstance(field, dict)]
        self.assertIn("sandbox_mode", field_keys)
        self.assertIn("fresh_thread_per_injection", field_keys)
        self.assertIn("claude_cli_path", field_keys)
        self.assertIn("claude_permission_mode", field_keys)
        self.assertIn("execution_mode", field_keys)
        self.assertIn("container_engine", field_keys)
        self.assertIn("container_image", field_keys)

    def test_slurm_launch_fields_include_claude_runtime(self):
        fields = _default_launch_fields_for_backend("slurm", {})
        worker_mode_field = next(
            (field for field in fields if field.get("key") == "worker_mode"),
            None,
        )
        self.assertIsNotNone(worker_mode_field)
        options = worker_mode_field.get("options") or []
        values = {option.get("value") for option in options if isinstance(option, dict)}
        self.assertIn("claude", values)
        field_keys = [field.get("key") for field in fields if isinstance(field, dict)]
        self.assertIn("approval_policy", field_keys)
        self.assertIn("fresh_thread_per_injection", field_keys)
        self.assertIn("claude_model", field_keys)
        self.assertIn("claude_cli_path", field_keys)
        self.assertIn("claude_permission_mode", field_keys)
        self.assertIn("pricing_model", field_keys)
        self.assertIn("execution_mode", field_keys)
        self.assertIn("container_engine", field_keys)
        self.assertIn("container_image", field_keys)
        container_engine_field = next((field for field in fields if field.get("key") == "container_engine"), None)
        self.assertIsNotNone(container_engine_field)
        self.assertIn(
            "apptainer",
            {option.get("value") for option in (container_engine_field.get("options") or []) if isinstance(option, dict)},
        )

    def test_launch_fields_use_configured_container_engine_defaults(self):
        fields = _default_launch_fields_for_backend(
            "slurm",
            {
                "default_execution_mode": "container",
                "default_container_engine": "apptainer",
                "supported_container_engines": ["apptainer", "docker"],
            },
        )
        execution_mode_field = next((field for field in fields if field.get("key") == "execution_mode"), None)
        container_engine_field = next((field for field in fields if field.get("key") == "container_engine"), None)
        self.assertEqual(execution_mode_field.get("default"), "container")
        self.assertEqual(container_engine_field.get("default"), "apptainer")
        self.assertEqual(
            {option.get("value") for option in (container_engine_field.get("options") or []) if isinstance(option, dict)},
            {"apptainer", "docker"},
        )

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

    def test_local_provider_launches_mock_worker_in_container_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = LocalProvider({"workspace_root": temp_dir})
            with patch.object(provider, "_ensure_local_container_image") as ensure_image:
                with patch.object(provider, "_start_container_worker", return_value="container-123"):
                    job_id = provider.launch(
                        1,
                        launch_params={
                            "worker_mode": "mock",
                            "execution_mode": "container",
                            "container_engine": "docker",
                        },
                    )
            metadata = provider._read_job_metadata(job_id) or {}
            self.assertEqual(metadata.get("execution_mode"), "container")
            self.assertEqual(metadata.get("container_engine"), "docker")
            self.assertEqual(metadata.get("container_image"), local_module.DEFAULT_LOCAL_CONTAINER_IMAGE)
            workers = metadata.get("workers") or []
            self.assertEqual(len(workers), 1)
            self.assertEqual(workers[0].get("container_id"), "container-123")
            ensure_image.assert_called_once()

    def test_local_provider_launches_native_worker_with_current_interpreter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = LocalProvider({"workspace_root": temp_dir})

            class FakeProc:
                pid = 4321

            with patch("router.providers.local.subprocess.Popen", return_value=FakeProc()) as popen_mock:
                with patch.object(provider, "_read_proc_start_ticks", return_value=123):
                    job_id = provider.launch(
                        1,
                        launch_params={
                            "worker_mode": "mock",
                            "execution_mode": "native",
                        },
                    )

            metadata = provider._read_job_metadata(job_id) or {}
            self.assertEqual(metadata.get("execution_mode"), "native")
            popen_args = popen_mock.call_args[0][0]
            self.assertEqual(popen_args[0], sys.executable)

    def test_local_container_mounts_make_repo_writable_when_workspace_is_nested(self):
        repo_root = Path(__file__).resolve().parents[1]
        workspace_root = repo_root / ".tmp" / "nested-workspace-test"
        provider = LocalProvider({"workspace_root": str(workspace_root)})
        mounts = provider._container_mounts()
        repo_mount = next((item for item in mounts if item[0] == repo_root.resolve()), None)
        self.assertIsNotNone(repo_mount)
        self.assertEqual(repo_mount[2], "rw")

    def test_local_prepare_repository_stages_local_source_for_container_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with tempfile.TemporaryDirectory() as source_dir:
                provider = LocalProvider({"workspace_root": temp_dir})
                source_repo = Path(source_dir) / "source-repo"
                subprocess.run(["git", "init", str(source_repo)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                subprocess.run(
                    ["git", "-C", str(source_repo), "config", "user.name", "Codeswarm Test"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                subprocess.run(
                    ["git", "-C", str(source_repo), "config", "user.email", "codeswarm@example.com"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                (source_repo / "README.md").write_text("hello\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(source_repo), "add", "README.md"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                subprocess.run(["git", "-C", str(source_repo), "commit", "-m", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

                job_id = "local_test_container"
                provider._write_job_metadata(
                    job_id,
                    {
                        "job_id": job_id,
                        "execution_mode": "container",
                        "workers": [{"container_id": "cid-1", "container_engine": "docker", "node_id": 0}],
                    },
                )
                with patch.object(provider, "_active_workers_for_job", return_value=[{"container_id": "cid-1", "container_engine": "docker", "node_id": 0}]):
                    prepared = provider.prepare_repository(job_id, str(source_repo))
                staged_source = prepared.get("source_path_staged")
                self.assertEqual(prepared.get("source"), str(source_repo.resolve()))
                self.assertTrue(isinstance(staged_source, str) and staged_source.endswith(f"{job_id}/source"))
                self.assertEqual(prepared.get("origin"), staged_source)
                self.assertTrue(Path(staged_source).exists())

    def test_local_prepare_repository_preserves_rw_mounted_local_origin_for_container_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = LocalProvider({"workspace_root": temp_dir})
            origin_repo = Path(temp_dir) / "origin.git"
            source_repo = Path(temp_dir) / "source-repo"
            subprocess.run(["git", "init", "--bare", str(origin_repo)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            subprocess.run(["git", "clone", str(origin_repo), str(source_repo)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            subprocess.run(
                ["git", "-C", str(source_repo), "config", "user.name", "Codeswarm Test"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(source_repo), "config", "user.email", "codeswarm@example.com"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            subprocess.run(["git", "-C", str(source_repo), "checkout", "-b", "main"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            (source_repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source_repo), "add", "README.md"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            subprocess.run(["git", "-C", str(source_repo), "commit", "-m", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            subprocess.run(["git", "-C", str(source_repo), "push", "-u", "origin", "main"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            job_id = "local_test_container_origin"
            provider._write_job_metadata(
                job_id,
                {
                    "job_id": job_id,
                    "execution_mode": "container",
                    "workers": [{"container_id": "cid-1", "container_engine": "docker", "node_id": 0}],
                },
            )
            with patch.object(provider, "_active_workers_for_job", return_value=[{"container_id": "cid-1", "container_engine": "docker", "node_id": 0}]):
                prepared = provider.prepare_repository(job_id, str(source_repo))
            self.assertEqual(Path(str(prepared.get("origin") or "")).resolve(), origin_repo.resolve())

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

    def test_aws_launch_fields_include_claude_env_profile_options(self):
        fields = _default_launch_fields_for_backend(
            "aws",
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

    def test_slurm_launch_fields_include_claude_env_profile_options(self):
        fields = _default_launch_fields_for_backend(
            "slurm",
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

    def test_aws_provider_applies_claude_env_profile_with_expansion(self):
        provider = AwsProvider(
            {
                "cluster": {
                    "workspace_root": "/srv",
                    "cluster_subdir": "codeswarm",
                    "aws": {
                        "region": "us-east-1",
                        "claude_env_profiles": {
                            "amd-llm-gateway": {
                                "ANTHROPIC_API_KEY": "dummy",
                                "ANTHROPIC_BASE_URL": "https://llm-api.amd.com/Anthropic",
                                "ANTHROPIC_CUSTOM_HEADERS": "Ocp-Apim-Subscription-Key: ${LLM_GATEWAY_KEY}",
                                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                            }
                        },
                    },
                }
            }
        )
        with patch.dict("os.environ", {"LLM_GATEWAY_KEY": "gateway-secret"}, clear=False):
            env = provider._resolve_claude_launch_env(
                {
                    "worker_mode": "claude",
                    "claude_env_profile": "amd-llm-gateway",
                }
            )
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "dummy")
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"), "https://llm-api.amd.com/Anthropic")
        self.assertEqual(
            env.get("ANTHROPIC_CUSTOM_HEADERS"),
            "Ocp-Apim-Subscription-Key: gateway-secret",
        )
        self.assertEqual(env.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"), "1")

    def test_aws_provider_rejects_claude_env_profile_when_placeholder_missing(self):
        provider = AwsProvider(
            {
                "cluster": {
                    "workspace_root": "/srv",
                    "cluster_subdir": "codeswarm",
                    "aws": {
                        "region": "us-east-1",
                        "claude_env_profiles": {
                            "amd-llm-gateway": {
                                "ANTHROPIC_CUSTOM_HEADERS": "Ocp-Apim-Subscription-Key: ${LLM_GATEWAY_KEY}",
                            }
                        },
                    },
                }
            }
        )
        with self.assertRaisesRegex(RuntimeError, "LLM_GATEWAY_KEY"):
            with patch.dict("os.environ", {}, clear=True):
                provider._resolve_claude_launch_env(
                    {
                        "worker_mode": "claude",
                        "claude_env_profile": "amd-llm-gateway",
                    }
                )

    def test_aws_provider_launches_codex_in_container_mode(self):
        provider = AwsProvider(
            {
                "cluster": {
                    "workspace_root": "/srv",
                    "cluster_subdir": "codeswarm",
                    "aws": {
                        "region": "us-east-1",
                        "ami_id": "ami-123",
                        "subnet_id": "subnet-123",
                        "key_name": "key",
                        "ssh_private_key_path": "~/.ssh/key.pem",
                    },
                }
            }
        )
        instance = {
            "InstanceId": "i-coord",
            "PrivateIpAddress": "10.0.0.5",
            "PublicIpAddress": "54.0.0.5",
            "Placement": {"AvailabilityZone": "us-east-1a"},
        }

        def fake_aws(args, expect_json=False):
            if list(args[:2]) == ["ec2", "create-volume"]:
                return {"VolumeId": "vol-123"}
            return {"ok": True} if expect_json else object()

        with patch.object(provider, "_verify_aws_auth"):
            with patch.object(provider, "_run_instances", side_effect=[["i-coord"], []]):
                with patch.object(provider, "_wait_instances_state"):
                    with patch.object(provider, "_get_instances_for_job", return_value=[instance]):
                        with patch.object(provider, "_aws", side_effect=fake_aws):
                            with patch.object(provider, "_wait_for_ssh"):
                                with patch.object(provider, "_setup_shared_ebs"):
                                    with patch.object(provider, "_sync_agent_dir"):
                                        with patch.object(provider, "_sync_container_assets") as sync_container_assets:
                                            with patch.object(provider, "_ensure_container_runtime") as ensure_runtime:
                                                with patch.object(provider, "_ensure_container_image") as ensure_image:
                                                    with patch.object(provider, "_prepare_run_directories"):
                                                        with patch.object(
                                                            provider,
                                                            "_start_codex_container_workers",
                                                            return_value=[
                                                                {
                                                                    "node_id": 0,
                                                                    "host": "54.0.0.5",
                                                                    "container_name": "codeswarm-aws_test-00",
                                                                    "container_engine": "docker",
                                                                    "container_image": AwsProvider.DEFAULT_CONTAINER_IMAGE,
                                                                }
                                                            ],
                                                        ):
                                                            with patch.object(provider, "_ensure_codex_tools") as ensure_codex_tools:
                                                                job_id = provider.launch(
                                                                    1,
                                                                    launch_params={
                                                                        "worker_mode": "codex",
                                                                        "execution_mode": "container",
                                                                        "container_engine": "docker",
                                                                        "instance_type": "c7i.4xlarge",
                                                                    },
                                                                )
        metadata = provider._get_job_meta(job_id) or {}
        self.assertEqual(metadata.get("execution_mode"), "container")
        self.assertEqual(metadata.get("container_engine"), "docker")
        self.assertEqual(metadata.get("container_image"), AwsProvider.DEFAULT_CONTAINER_IMAGE)
        self.assertEqual(len(metadata.get("workers") or []), 1)
        sync_container_assets.assert_called_once_with(["54.0.0.5"])
        ensure_runtime.assert_called_once_with("54.0.0.5", "docker")
        ensure_image.assert_called_once_with("54.0.0.5", "docker", AwsProvider.DEFAULT_CONTAINER_IMAGE, "if_not_present")
        ensure_codex_tools.assert_not_called()

    def test_aws_provider_builds_codex_container_launch_script(self):
        provider = AwsProvider(
            {
                "cluster": {
                    "workspace_root": "/srv",
                    "cluster_subdir": "codeswarm",
                    "aws": {
                        "region": "us-east-1",
                    },
                }
            }
        )
        captured: dict[str, object] = {}

        def fake_ssh_with_env(host, env_vars, remote_script):
            captured["host"] = host
            captured["env"] = dict(env_vars)
            captured["script"] = remote_script
            return subprocess.CompletedProcess([], 0, "", "")

        with patch.object(provider, "_local_openai_api_key", return_value="openai-key"):
            with patch.object(provider, "_local_github_token", return_value=""):
                with patch.object(provider, "_ssh_with_env", side_effect=fake_ssh_with_env):
                    workers = provider._start_codex_container_workers(
                        {"host-1": [0]},
                        "awsjob",
                        {
                            "execution_mode": "container",
                            "container_engine": "docker",
                            "container_image": "ghcr.io/kalowery/codeswarm-local-worker:latest",
                            "approval_policy": "never",
                            "sandbox_mode": "workspace-write",
                            "fresh_thread_per_injection": False,
                        },
                    )
        self.assertEqual(captured.get("host"), "host-1")
        env = captured.get("env") or {}
        script = str(captured.get("script") or "")
        self.assertEqual(env.get("OPENAI_API_KEY"), "openai-key")
        self.assertIn('*docker_cmd, "run", "-d"', script)
        self.assertIn('"--preserve-env=" + ",".join(preserve_env)', script)
        self.assertIn('"--user", str(uid) + ":" + str(gid)', script)
        self.assertIn("codex login status", script)
        self.assertIn("agent/codex_worker.py", script)
        self.assertIn('"CODESWARM_ASK_FOR_APPROVAL=" + "never"', script)
        self.assertIn('sandbox_mode = "workspace-write"', script)
        self.assertIn("export BASE JOB ENGINE IMAGE WORKERS_JSON", script)
        python_body = script.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        compile(python_body, "<aws-codex-container-script>", "exec")
        self.assertEqual(
            workers,
            [
                {
                    "node_id": 0,
                    "host": "host-1",
                    "container_name": provider._container_name("awsjob", 0),
                    "container_engine": "docker",
                    "container_image": "ghcr.io/kalowery/codeswarm-local-worker:latest",
                }
            ],
        )

    def test_aws_provider_uses_github_auth_for_ghcr_image_pulls(self):
        provider = AwsProvider(
            {
                "cluster": {
                    "workspace_root": "/srv",
                    "cluster_subdir": "codeswarm",
                    "aws": {
                        "region": "us-east-1",
                    },
                }
            }
        )
        captured: dict[str, object] = {}

        def fake_ssh_with_env(host, env_vars, remote_script):
            captured["host"] = host
            captured["env"] = dict(env_vars)
            captured["script"] = remote_script
            return subprocess.CompletedProcess([], 0, "", "")

        with patch.object(provider, "_local_github_token", return_value="gh-token"):
            with patch.object(provider, "_local_github_username", return_value="kalowery"):
                with patch.object(provider, "_ssh_with_env", side_effect=fake_ssh_with_env):
                    with patch.object(provider, "_ssh") as ssh_mock:
                        provider._ensure_container_image(
                            "host-1",
                            "docker",
                            "ghcr.io/kalowery/codeswarm-local-worker:latest",
                            "if_not_present",
                        )
        self.assertEqual(captured.get("host"), "host-1")
        self.assertEqual((captured.get("env") or {}).get("GITHUB_TOKEN"), "gh-token")
        self.assertEqual((captured.get("env") or {}).get("GITHUB_USERNAME"), "kalowery")
        script = str(captured.get("script") or "")
        self.assertIn("$ENGINE login ghcr.io", script)
        self.assertIn('printf \'%s\' "$GITHUB_TOKEN"', script)
        ssh_mock.assert_not_called()

    def test_aws_provider_builds_default_image_when_pull_unavailable(self):
        provider = AwsProvider(
            {
                "cluster": {
                    "workspace_root": "/srv",
                    "cluster_subdir": "codeswarm",
                    "aws": {
                        "region": "us-east-1",
                    },
                }
            }
        )
        captured: dict[str, object] = {}

        def fake_ssh_with_env(host, env_vars, remote_script):
            captured["host"] = host
            captured["env"] = dict(env_vars)
            captured["script"] = remote_script
            return subprocess.CompletedProcess([], 0, "", "")

        with patch.object(provider, "_local_github_token", return_value="gh-token"):
            with patch.object(provider, "_local_github_username", return_value="kalowery"):
                with patch.object(provider, "_ssh_with_env", side_effect=fake_ssh_with_env):
                    provider._ensure_container_image(
                        "host-1",
                        "docker",
                        AwsProvider.DEFAULT_CONTAINER_IMAGE,
                        "if_not_present",
                    )
        script = str(captured.get("script") or "")
        self.assertIn("build_default_image()", script)
        self.assertIn("$ENGINE build -t \"$IMAGE\" -f \"$DEFAULT_DOCKERFILE\" \"$DEFAULT_CONTEXT\"", script)
        self.assertIn(f"DEFAULT_DOCKERFILE={provider._quote(provider.base_path + '/docker/local-worker.Dockerfile')}", script)

    def test_aws_provider_builds_claude_container_launch_script_with_non_root_user(self):
        provider = AwsProvider(
            {
                "cluster": {
                    "workspace_root": "/srv",
                    "cluster_subdir": "codeswarm",
                    "aws": {
                        "region": "us-east-1",
                    },
                }
            }
        )
        captured: dict[str, object] = {}

        def fake_ssh_with_env(host, env_vars, remote_script):
            captured["host"] = host
            captured["env"] = dict(env_vars)
            captured["script"] = remote_script
            return subprocess.CompletedProcess([], 0, "", "")

        with patch.object(provider, "_resolve_claude_launch_env", return_value={"ANTHROPIC_API_KEY": "anthropic-key"}):
            with patch.object(provider, "_local_github_token", return_value="gh-token"):
                with patch.object(provider, "_ssh_with_env", side_effect=fake_ssh_with_env):
                    workers = provider._start_claude_container_workers(
                        {"host-1": [0]},
                        "awsjob",
                        {
                            "execution_mode": "container",
                            "container_engine": "docker",
                            "container_image": "ghcr.io/kalowery/codeswarm-local-worker:latest",
                            "approval_policy": "never",
                        },
                    )
        script = str(captured.get("script") or "")
        self.assertIn('"--preserve-env=" + ",".join(preserve_env)', script)
        self.assertIn('"--user", str(uid) + ":" + str(gid)', script)
        self.assertIn('"HOME=" + container_home', script)
        self.assertEqual((captured.get("env") or {}).get("ANTHROPIC_API_KEY"), "anthropic-key")
        self.assertEqual(
            workers,
            [
                {
                    "node_id": 0,
                    "host": "host-1",
                    "container_name": provider._container_name("awsjob", 0),
                    "container_engine": "docker",
                    "container_image": "ghcr.io/kalowery/codeswarm-local-worker:latest",
                }
            ],
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

    def test_router_resolves_aws_claude_profile_model_for_agent_and_pricing(self):
        config = {
            "cluster": {
                "backend": "aws",
                "aws": {
                    "region": "us-east-1",
                    "claude_env_profiles": {
                        "amd-llm-gateway": {
                            "ANTHROPIC_MODEL": "Claude-Sonnet-4.5",
                        }
                    },
                },
            }
        }
        params = {
            "worker_mode": "claude",
            "claude_env_profile": "amd-llm-gateway",
        }
        agent_model = router_module._resolve_swarm_agent_model(
            config,
            "claude",
            params,
            provider_backend="aws",
        )
        pricing_model = router_module._resolve_swarm_pricing_model(
            config,
            "claude",
            params,
            provider_backend="aws",
        )
        self.assertEqual(agent_model, "Claude-Sonnet-4.5")
        self.assertEqual(pricing_model, "Claude-Sonnet-4.5")

    def test_slurm_provider_resolves_claude_profile_env_and_defaults_permission_mode(self):
        provider = SlurmProvider(
            {
                "cluster": {
                    "slurm": {
                        "login_host": "cluster-login",
                        "claude_env_profiles": {
                            "amd-llm-gateway": {
                                "ANTHROPIC_BASE_URL": "${TEST_ANTHROPIC_BASE_URL}",
                                "ANTHROPIC_MODEL": "Claude-Sonnet-4.5",
                            }
                        },
                    }
                }
            }
        )
        with patch.dict(
            "os.environ",
            {
                "TEST_ANTHROPIC_BASE_URL": "https://llm-api.amd.com/Anthropic",
                "TEST_ANTHROPIC_AUTH_TOKEN": "token-123",
            },
            clear=False,
        ):
            env = provider._resolve_claude_launch_env(
                {
                    "claude_env_profile": "amd-llm-gateway",
                    "claude_env": {
                        "ANTHROPIC_AUTH_TOKEN": "${TEST_ANTHROPIC_AUTH_TOKEN}",
                    },
                }
            )
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"), "https://llm-api.amd.com/Anthropic")
        self.assertEqual(env.get("ANTHROPIC_MODEL"), "Claude-Sonnet-4.5")
        self.assertEqual(env.get("ANTHROPIC_AUTH_TOKEN"), "token-123")
        self.assertEqual(provider._claude_permission_mode({"approval_policy": "never"}), "bypassPermissions")
        self.assertEqual(provider._claude_permission_mode({"approval_policy": "on-request"}), "default")
        self.assertEqual(provider._execution_mode({"execution_mode": "container"}), "container")
        self.assertEqual(provider._container_engine({}), "apptainer")

    def test_slurm_provider_rejects_container_execution_until_implemented(self):
        provider = SlurmProvider(
            {
                "cluster": {
                    "slurm": {
                        "login_host": "cluster-login",
                        "partition": "cpu",
                        "time_limit": "00:30:00",
                    }
                }
            }
        )
        with self.assertRaisesRegex(RuntimeError, "container execution is not implemented yet"):
            provider.launch(
                1,
                launch_params={
                    "worker_mode": "claude",
                    "execution_mode": "container",
                    "container_engine": "apptainer",
                },
            )

    def test_router_resolves_slurm_claude_profile_model_for_agent_and_pricing(self):
        config = {
            "cluster": {
                "backend": "slurm",
                "slurm": {
                    "login_host": "cluster-login",
                    "claude_env_profiles": {
                        "amd-llm-gateway": {
                            "ANTHROPIC_MODEL": "Claude-Sonnet-4.5",
                        }
                    },
                },
            }
        }
        params = {
            "worker_mode": "claude",
            "claude_env_profile": "amd-llm-gateway",
        }
        agent_model = router_module._resolve_swarm_agent_model(
            config,
            "claude",
            params,
            provider_backend="slurm",
        )
        pricing_model = router_module._resolve_swarm_pricing_model(
            config,
            "claude",
            params,
            provider_backend="slurm",
        )
        self.assertEqual(agent_model, "Claude-Sonnet-4.5")
        self.assertEqual(pricing_model, "Claude-Sonnet-4.5")

    def test_slurm_prepare_repository_for_github_origin_uses_shared_clone_contract(self):
        provider = SlurmProvider(
            {
                "cluster": {
                    "workspace_root": "/srv",
                    "cluster_subdir": "codeswarm",
                    "slurm": {
                        "login_host": "cluster-login",
                    },
                }
            }
        )
        scripts: list[str] = []

        class _Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_ssh_run(args, timeout=None, input_text=None):
            remote_cmd = args[-1]
            scripts.append(remote_cmd)
            if "for d in \"$BASE/runs/$JOB\"/agent_*" in remote_cmd:
                return _Result(stdout="00\n01\n")
            return _Result()

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir) / "repo"
            subprocess.run(["git", "init", str(repo_dir)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            subprocess.run(
                ["git", "-C", str(repo_dir), "remote", "add", "origin", "https://github.com/example/project.git"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            with patch.object(provider, "_ssh_run", side_effect=fake_ssh_run):
                with patch.object(provider, "_rsync_to_login_host") as rsync_mock:
                    with patch.object(provider, "_local_github_token", return_value="ghs_test_token"):
                        with patch.object(provider, "_stage_github_token_file", return_value="/srv/codeswarm/runtime/github-token.txt"):
                            with patch.object(provider, "_checkout_prepared_branch_remote") as checkout_mock:
                                prepared = provider.prepare_repository(
                                    "12345",
                                    str(repo_dir),
                                    branch="main",
                                    subdir="repo",
                                )

        self.assertEqual(prepared["mode"], "per_agent_clone")
        self.assertEqual(prepared["source_kind"], "local_path")
        self.assertEqual(prepared["origin"], "https://github.com/example/project.git")
        self.assertEqual(
            prepared["worker_paths"],
            [
                "/srv/codeswarm/runs/12345/agent_00/repo",
                "/srv/codeswarm/runs/12345/agent_01/repo",
            ],
        )
        rsync_mock.assert_called_once_with(repo_dir.resolve(), "/srv/codeswarm/project_sources/12345/source")
        self.assertEqual(checkout_mock.call_count, 2)
        joined = "\n".join(scripts)
        self.assertIn("/srv/codeswarm/runtime/github-token.txt", joined)
        self.assertIn("credential.helper", joined)
        self.assertIn("https://github.com/example/project.git", joined)

    def test_slurm_provider_launch_threads_claude_runtime_args(self):
        provider = SlurmProvider(
            {
                "cluster": {
                    "slurm": {
                        "login_host": "cluster-login",
                        "partition": "cpu",
                        "time_limit": "00:30:00",
                    }
                }
            }
        )

        captured = {}

        class _DummyProc:
            def __init__(self):
                self.stdout = iter(["JOB_ID=12345\n"])

            def wait(self):
                return 0

        def fake_popen(cmd, stdout=None, stderr=None, text=None, bufsize=None):
            captured["cmd"] = list(cmd)
            return _DummyProc()

        with patch("router.providers.slurm.subprocess.Popen", side_effect=fake_popen):
            with patch.object(provider, "_stage_claude_env_file", return_value="/srv/codeswarm/runtime/claude-launch.sh"):
                job_id = provider.launch(
                    2,
                    launch_params={
                        "worker_mode": "claude",
                        "approval_policy": "on-request",
                        "fresh_thread_per_injection": True,
                        "claude_model": "Claude-Sonnet-4.5",
                        "claude_cli_path": "/opt/claude/bin/claude",
                    },
                )

        self.assertEqual(job_id, "12345")
        cmd = captured["cmd"]
        self.assertIn("--launch-worker-run", cmd)
        self.assertIn("--worker-mode", cmd)
        self.assertIn("claude", cmd)
        self.assertIn("--approval-policy", cmd)
        self.assertIn("on-request", cmd)
        self.assertIn("--fresh-thread-per-injection", cmd)
        self.assertIn("--claude-model", cmd)
        self.assertIn("Claude-Sonnet-4.5", cmd)
        self.assertIn("--claude-cli-path", cmd)
        self.assertIn("/opt/claude/bin/claude", cmd)
        self.assertIn("--claude-permission-mode", cmd)
        self.assertIn("--claude-env-file", cmd)
        self.assertIn("/srv/codeswarm/runtime/claude-launch.sh", cmd)
        self.assertNotIn("--launch-codex-run", cmd)

    def test_slurm_allocate_builds_claude_worker_script_from_runtime_args(self):
        args = slurm_allocate_module.argparse.Namespace(
            nodes=2,
            time="00:30:00",
            partition="cpu",
            account=None,
            qos=None,
            approval_policy="on-request",
            fresh_thread_per_injection="1",
            claude_model="Claude-Sonnet-4.5",
            claude_cli_path="/opt/claude/bin/claude",
            claude_permission_mode="default",
            claude_env_file="/srv/codeswarm/runtime/claude-launch.sh",
            launch_worker_run=True,
            launch_codex_run=False,
            launch_codex_test=False,
            worker_mode="claude",
        )
        script = slurm_allocate_module.build_sbatch_script(
            args,
            {
                "cluster": {
                    "workspace_root": "/srv",
                    "cluster_subdir": "codeswarm",
                    "slurm": {},
                }
            },
        )
        self.assertIn('CLAUDE_VENV="/srv/codeswarm/tools/claude-venv"', script)
        self.assertIn('export PATH="$CLAUDE_VENV/bin:$PATH"', script)
        self.assertIn('"$CLAUDE_VENV/bin/python" "/srv/codeswarm/agent/claude_worker.py"', script)
        self.assertIn(". /srv/codeswarm/runtime/claude-launch.sh", script)
        self.assertIn("export CODESWARM_ASK_FOR_APPROVAL=on-request", script)
        self.assertIn("export CODESWARM_CLAUDE_PERMISSION_MODE=default", script)
        self.assertIn("export CODESWARM_CLAUDE_MODEL=Claude-Sonnet-4.5", script)
        self.assertIn("export CODESWARM_CLAUDE_CLI_PATH=/opt/claude/bin/claude", script)
        self.assertIn("export CODESWARM_FRESH_THREAD_PER_INJECTION=1", script)
        self.assertNotIn("codex_worker.py", script)

    def test_slurm_provider_stages_claude_env_file_without_putting_values_in_path(self):
        provider = SlurmProvider(
            {
                "cluster": {
                    "workspace_root": "/srv",
                    "cluster_subdir": "codeswarm",
                    "slurm": {
                        "login_host": "cluster-login",
                    },
                }
            }
        )
        captured = {}

        def fake_ssh_run(args, timeout=None, input_text=None):
            captured["args"] = list(args)
            captured["input_text"] = input_text

            class _Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Result()

        with patch.object(provider, "_resolve_claude_launch_env", return_value={"ANTHROPIC_API_KEY": "secret-token"}):
            with patch.object(provider, "_ssh_run", side_effect=fake_ssh_run):
                remote_path = provider._stage_claude_env_file({"worker_mode": "claude"})

        self.assertIsNotNone(remote_path)
        self.assertIn("/srv/codeswarm/runtime/claude-env-", str(remote_path))
        self.assertNotIn("secret-token", " ".join(captured["args"]))
        self.assertEqual(captured["input_text"], "export ANTHROPIC_API_KEY=secret-token\n")


if __name__ == "__main__":
    unittest.main()
