from __future__ import annotations
import unittest
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.twinverse_anchor_patch import apply_anchor_bookkeeping_patch


@dataclass
class PathCache:
    targets: list = field(default_factory=list)


class TwinverseCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_plans_rediscover_unity_and_anchor_counts_match(self):
        publisher = SimpleNamespace(_auto_discover_runtime=True, tenant_id="SKKU", scene_id="RobotRoom", discovery_timeout=1, runtime_code="old")
        module = SimpleNamespace(global_context=SimpleNamespace(robot_mqtt_publisher=publisher), _paths={}, _Path=PathCache,
            _Target=lambda **data:data, _resolve_object_id=lambda payload:"target", _require_str=lambda payload,key:payload[key])
        async def manage(action, payload):
            if action == "make_path": module._paths[("target",payload["name"])] = PathCache()
            return {"success":True,"data":{}}
        module._manage_path = manage
        discovery = Mock(side_effect=["runtime-A","runtime-B"])
        with patch("tools.twinverse_anchor_patch.importlib.import_module", return_value=module), patch.dict(sys.modules, {"twinverse.mcp_server.robot_mqtt":SimpleNamespace(discover_runtime_code=discovery)}):
            apply_anchor_bookkeeping_patch()
            await module._manage_path("make_path", {"name":"path-A"})
            self.assertEqual(publisher.runtime_code,"runtime-A")
            for index in range(4):
                result = await module._manage_path("make_object_anchor_target", {"path":"path-A","name":str(index)})
                self.assertEqual(result["data"]["target_count"],index+1)
            await module._manage_path("make_path", {"name":"path-B"})
            self.assertEqual(publisher.runtime_code,"runtime-B")
            self.assertEqual(discovery.call_count,2)
            self.assertEqual(len(module._paths[("target","path-B")].targets),0)


if __name__ == "__main__": unittest.main()
