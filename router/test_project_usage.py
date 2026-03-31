import unittest

from router import router as router_module


class ProjectUsageAccountingTests(unittest.TestCase):
    def test_project_usage_counts_last_usage_not_cumulative_thread_total(self):
        original_projects = router_module.PROJECTS
        original_swarms = router_module.SWARMS
        try:
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


if __name__ == "__main__":
    unittest.main()
