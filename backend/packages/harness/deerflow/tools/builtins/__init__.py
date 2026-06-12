from .clarification_tool import ask_clarification_tool
from .present_file_tool import present_file_tool
from .setup_agent_tool import setup_agent
from .update_agent_tool import update_agent
from .view_image_tool import view_image_tool

__all__ = [
    "setup_agent",
    "update_agent",
    "present_file_tool",
    "ask_clarification_tool",
    "view_image_tool",
    "task_tool",
]

# task_tool is lazily loaded to break the import cycle:
#   subagents → executor → agents(thread_state) → factory → tools(builtins) → task_tool → subagents
# Importing task_tool at the package level forces subagents to load before
# deerflow.agents is fully initialized, causing "cannot import name
# 'SubagentExecutor' from partially initialized module".
# Moving to __getattr__ defers the import until task_tool is actually accessed
# (inside factory._assemble_from_features), by which time all packages are ready.


def __getattr__(name: str):
    if name == "task_tool":
        from .task_tool import task_tool

        return task_tool
    raise AttributeError(name)
