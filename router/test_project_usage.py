import unittest

from router import router as router_module


class ProjectUsageAccountingTests(unittest.TestCase):
    def test_project_usage_counts_last_usage_not_cumulative_thread_total(self):
        original_projects = router_module.PROJECTS
        original_swarms = router_module.SWARMS
        original_model_pricing = router_module.MODEL_PRICING
        try:
            router_module.MODEL_PRICING = {
                "gpt-5-4": {
                    "model_name": "gpt-5.4",
                    "input_tokens_usd_per_m": 2.5,
                    "cached_input_tokens_usd_per_m": 0.25,
                    "output_tokens_usd_per_m": 15.0,
                    "reasoning_output_tokens_usd_per_m": 0.0,
                }
            }
            router_module.PROJECTS = {
                "project-1": {
                    "project_id": "project-1",
                    "usage": router_module._empty_usage_totals(),
                    "worker_usage": {},
                    "tasks": {
                        "T-001": {
                            "task_id": "T-001",
                            "usage": router_module._empty_usage_totals(),
                            "active_attempt_usage": None,
                            "assignment_injection_id": "inj-1",
                            "assigned_swarm_id": "swarm-1",
                        },
                        "T-002": {
                            "task_id": "T-002",
                            "usage": router_module._empty_usage_totals(),
                            "active_attempt_usage": None,
                            "assignment_injection_id": "inj-2",
                            "assigned_swarm_id": "swarm-1",
                        },
                    },
                }
            }
            router_module.SWARMS = {
                "swarm-1": {
                    "swarm_id": "swarm-1",
                    "alias": "worker-a",
                }
            }

            changed = router_module._update_project_usage_for_injection(
                {
                    "injection_id": "inj-1",
                    "swarm_id": "swarm-1",
                    "node_id": 0,
                    "total_tokens": 100,
                    "input_tokens": 60,
                    "output_tokens": 40,
                    "cached_input_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "last_total_tokens": 100,
                    "last_input_tokens": 60,
                    "last_output_tokens": 40,
                    "last_cached_input_tokens": 0,
                    "last_reasoning_output_tokens": 0,
                    "usage_source": "thread/tokenUsage/updated",
                }
            )
            self.assertTrue(changed)

            changed = router_module._update_project_usage_for_injection(
                {
                    "injection_id": "inj-2",
                    "swarm_id": "swarm-1",
                    "node_id": 0,
                    "total_tokens": 150,
                    "input_tokens": 90,
                    "output_tokens": 60,
                    "cached_input_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "last_total_tokens": 50,
                    "last_input_tokens": 30,
                    "last_output_tokens": 20,
                    "last_cached_input_tokens": 0,
                    "last_reasoning_output_tokens": 0,
                    "usage_source": "thread/tokenUsage/updated",
                }
            )
            self.assertTrue(changed)

            project = router_module.PROJECTS["project-1"]
            task_1 = project["tasks"]["T-001"]
            task_2 = project["tasks"]["T-002"]
            worker = project["worker_usage"]["swarm-1:0"]

            self.assertEqual(task_1["usage"]["total_tokens"], 100)
            self.assertEqual(task_2["usage"]["total_tokens"], 50)
            self.assertEqual(project["usage"]["total_tokens"], 150)
            self.assertEqual(project["usage"]["input_tokens"], 90)
            self.assertEqual(project["usage"]["output_tokens"], 60)
            self.assertEqual(worker["usage"]["total_tokens"], 150)
            self.assertEqual(worker["usage"]["input_tokens"], 90)
            self.assertEqual(worker["usage"]["output_tokens"], 60)
        finally:
            router_module.PROJECTS = original_projects
            router_module.SWARMS = original_swarms
            router_module.MODEL_PRICING = original_model_pricing

    def test_project_usage_accumulates_cost_across_different_pricing_models(self):
        original_projects = router_module.PROJECTS
        original_swarms = router_module.SWARMS
        original_model_pricing = router_module.MODEL_PRICING
        try:
            router_module.MODEL_PRICING = {
                "gpt-5-4": {
                    "model_name": "gpt-5.4",
                    "input_tokens_usd_per_m": 2.5,
                    "cached_input_tokens_usd_per_m": 0.25,
                    "output_tokens_usd_per_m": 15.0,
                    "reasoning_output_tokens_usd_per_m": 0.0,
                },
                "claude-sonnet-4-5": {
                    "model_name": "Claude-Sonnet-4.5",
                    "input_tokens_usd_per_m": 3.0,
                    "cached_input_tokens_usd_per_m": 0.3,
                    "output_tokens_usd_per_m": 15.0,
                    "reasoning_output_tokens_usd_per_m": 0.0,
                },
            }
            router_module.PROJECTS = {
                "project-1": {
                    "project_id": "project-1",
                    "usage": router_module._empty_usage_totals(),
                    "worker_usage": {},
                    "tasks": {
                        "T-001": {
                            "task_id": "T-001",
                            "usage": router_module._empty_usage_totals(),
                            "active_attempt_usage": None,
                            "assignment_injection_id": "inj-1",
                            "assigned_swarm_id": "swarm-1",
                        },
                        "T-002": {
                            "task_id": "T-002",
                            "usage": router_module._empty_usage_totals(),
                            "active_attempt_usage": None,
                            "assignment_injection_id": "inj-2",
                            "assigned_swarm_id": "swarm-2",
                        },
                    },
                }
            }
            router_module.SWARMS = {
                "swarm-1": {
                    "swarm_id": "swarm-1",
                    "alias": "worker-a",
                    "pricing_model": "gpt-5.4",
                    "agent_model": "gpt-5.4",
                },
                "swarm-2": {
                    "swarm_id": "swarm-2",
                    "alias": "worker-b",
                    "pricing_model": "Claude-Sonnet-4.5",
                    "agent_model": "Claude-Sonnet-4.5",
                },
            }

            first_usage = router_module._normalize_usage_payload(
                {"swarm_id": "swarm-1", "node_id": 0, "injection_id": "inj-1"},
                {
                    "total_tokens": 100,
                    "input_tokens": 60,
                    "cached_input_tokens": 0,
                    "output_tokens": 40,
                    "reasoning_output_tokens": 0,
                },
                {
                    "total_tokens": 100,
                    "input_tokens": 60,
                    "cached_input_tokens": 0,
                    "output_tokens": 40,
                    "reasoning_output_tokens": 0,
                },
                None,
                "thread/tokenUsage/updated",
            )
            second_usage = router_module._normalize_usage_payload(
                {"swarm_id": "swarm-2", "node_id": 1, "injection_id": "inj-2"},
                {
                    "total_tokens": 200,
                    "input_tokens": 150,
                    "cached_input_tokens": 50,
                    "output_tokens": 50,
                    "reasoning_output_tokens": 0,
                },
                {
                    "total_tokens": 200,
                    "input_tokens": 150,
                    "cached_input_tokens": 50,
                    "output_tokens": 50,
                    "reasoning_output_tokens": 0,
                },
                None,
                "thread/tokenUsage/updated",
            )

            self.assertTrue(router_module._update_project_usage_for_injection(first_usage))
            self.assertTrue(router_module._update_project_usage_for_injection(second_usage))

            project = router_module.PROJECTS["project-1"]
            task_1 = project["tasks"]["T-001"]
            task_2 = project["tasks"]["T-002"]

            self.assertEqual(task_1["usage"]["pricing_model"], "gpt-5.4")
            self.assertEqual(task_2["usage"]["pricing_model"], "Claude-Sonnet-4.5")
            self.assertEqual(project["usage"]["pricing_model"], "mixed")
            self.assertAlmostEqual(task_1["usage"]["estimated_cost_usd"], 0.00075, places=12)
            self.assertAlmostEqual(task_2["usage"]["estimated_cost_usd"], 0.001065, places=12)
            self.assertAlmostEqual(project["usage"]["estimated_cost_usd"], 0.001815, places=12)
        finally:
            router_module.PROJECTS = original_projects
            router_module.SWARMS = original_swarms
            router_module.MODEL_PRICING = original_model_pricing


if __name__ == "__main__":
    unittest.main()
