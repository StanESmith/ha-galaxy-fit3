"""The service handlers in __init__.py must only use attributes the coordinator really has.

Source-only on purpose: the test environment does not install Home Assistant.
"""
import ast
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "galaxy_fit3"


def _coordinator_members() -> set[str]:
    tree = ast.parse((PKG / "coordinator.py").read_text())
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Fit3Coordinator")
    members = set()
    for node in ast.walk(cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            members.add(node.name)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            if isinstance(node.ctx, ast.Store):
                members.add(node.attr)
    return members


def test_coordinator_attributes_used_by_services_exist():
    used = set(re.findall(r"\bcoordinator\.([A-Za-z_][A-Za-z0-9_]*)", (PKG / "__init__.py").read_text()))
    assert used, "expected __init__.py to use the coordinator"
    inherited = {"data", "last_update_success", "async_request_refresh", "async_set_updated_data",
                 "async_config_entry_first_refresh", "async_shutdown", "async_refresh", "hass", "name"}   # DataUpdateCoordinator
    missing = sorted(used - _coordinator_members() - inherited)
    assert not missing, f"__init__.py uses coordinator attributes that do not exist: {missing}"


def test_client_is_exposed_as_a_property():
    src = (PKG / "coordinator.py").read_text()
    assert re.search(r"@property\s+def client\(self\)", src)
