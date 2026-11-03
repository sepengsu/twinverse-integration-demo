"""Prompt and safety constants for the SKKU robot agent."""

SYSTEM_CAPABILITIES = (
    "robot_path_planning",
    "robot_state_query",
    "path_validation",
)

BLOCKED_EXECUTION_ACTIONS = {
    "start_path",
    "move_tcp",
    "move_tcp_to_object_anchor",
    "homing",
    "simulation_start",
    "real_start",
    "run_physical_trajectory",
    "start_trajectory",
    "drive_to_object_anchor",
}

SAFE_PLAN_ONLY_MANAGE_PATH_ACTIONS = {
    "make_path",
    "make_target",
    "make_object_anchor_target",
    "connect_targets",
}
