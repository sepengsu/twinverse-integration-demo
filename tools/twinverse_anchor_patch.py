"""Compatibility adapter for Twinverse's missing object-anchor path bookkeeping.

The authoritative poses remain in Unity. Empty coordinates explicitly indicate
that the Python cache only tracks identity and count, never inferred geometry.
"""
import importlib
import asyncio


def apply_anchor_bookkeeping_patch():
    module = importlib.import_module("twinverse.mcp_server.tools.manage_path")
    original = module._manage_path

    async def manage(action, payload):
        if action == "make_path":
            # A connected publisher otherwise keeps the first runtime code forever.
            # Resolve again at each new plan; do not retarget half a published path.
            context = module.global_context
            publisher = context.robot_mqtt_publisher
            if publisher._auto_discover_runtime:
                from twinverse.mcp_server.robot_mqtt import discover_runtime_code
                publisher.runtime_code = await asyncio.to_thread(discover_runtime_code, publisher.tenant_id, publisher.scene_id, timeout=publisher.discovery_timeout)
        result = await original(action, payload)
        if action == "make_object_anchor_target" and result.get("success"):
            object_id = module._resolve_object_id(payload)
            path_name = module._require_str(payload, "path")
            path = module._paths.setdefault((object_id, path_name), module._Path())
            name = payload.get("name") or f"Anchor{len(path.targets)}"
            path.targets.append(module._Target(name=name, position=[], rotation_xyzw=[]))
            result.setdefault("data", {}).update(path=path_name, target=name, target_count=len(path.targets), pose_source="unity_authoritative")
        return result

    module._manage_path = manage
