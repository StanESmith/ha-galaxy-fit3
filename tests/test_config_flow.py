"""Drive the config flow's state machine with stand-ins for Home Assistant and the Bluetooth stack.

Only the flow's own control logic is under test: progress -> show code -> confirm/reject -> finish.
config_flow.py is loaded under a throwaway package with ``ble_support`` and ``bluez_agent`` replaced
by fakes, and the four Home Assistant / voluptuous imports stubbed.
"""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types

_PKG_DIR = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "galaxy_fit3"


def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class _Required:
    def __init__(self, key, default=None):
        self.key, self.default = key, default


class _FlowBase:
    def __init_subclass__(cls, domain=None, **kw):
        pass

    context: dict = {}

    async def async_set_unique_id(self, uid):
        self.unique_id = uid

    def _abort_if_unique_id_configured(self):
        pass

    def async_show_form(self, **kw):
        return {"type": "form", **kw}

    def async_show_progress(self, **kw):
        return {"type": "progress", **kw}

    def async_show_progress_done(self, **kw):
        return {"type": "progress_done", **kw}

    def async_abort(self, **kw):
        return {"type": "abort", **kw}

    def async_create_entry(self, **kw):
        return {"type": "create_entry", **kw}


class FakeAgent:
    """Replaces PairingAgent: remembers the confirm callback so the fake client can call it."""

    confirm = None

    def __init__(self, capability="DisplayYesNo", confirm=None):
        self._confirm = confirm

    async def __aenter__(self):
        FakeAgent.confirm = self._confirm
        return self

    async def __aexit__(self, *exc):
        FakeAgent.confirm = None
        return False


class FakeClient:
    code_needed = True

    async def connect(self):
        if FakeClient.code_needed and FakeAgent.confirm is not None:
            if not await FakeAgent.confirm("/dev/x", 12345):
                raise RuntimeError("rejected by user")
        await asyncio.sleep(0)

    async def disconnect(self):
        pass


def _load():
    _stub("voluptuous", Schema=lambda d: d, Required=_Required, Optional=_Required)
    _stub("homeassistant")
    _stub("homeassistant.components")
    _stub("homeassistant.components.bluetooth", BluetoothServiceInfoBleak=object)
    _stub("homeassistant.config_entries", ConfigFlow=_FlowBase, ConfigFlowResult=dict, OptionsFlow=_FlowBase, ConfigEntry=object)
    _stub("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _stub("homeassistant.helpers")
    _stub(
        "homeassistant.helpers.selector",
        NumberSelector=lambda config: config,
        NumberSelectorConfig=lambda **kwargs: kwargs,
        NumberSelectorMode=types.SimpleNamespace(BOX="box"),
    )
    pkg = types.ModuleType("gf3f")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["gf3f"] = pkg

    async def _noop(address):
        return None

    _stub("gf3f.ble_support", build_client=lambda hass, address, on_disconnect=None: FakeClient(),
          prefer_le_bearer=_noop, forget_bond=_noop)
    _stub("gf3f.bluez_agent", PairingAgent=FakeAgent)
    return importlib.import_module("gf3f.config_flow")


cf = _load()


def _flow():
    flow = cf.Fit3ConfigFlow()
    flow.hass = types.SimpleNamespace(async_create_task=lambda coro, name=None: asyncio.ensure_future(coro))
    flow.context = {}
    return flow


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


USER = {"address": "aa:bb:cc:dd:ee:ff", "name": "Watch"}


def test_full_path_shows_code_then_creates_entry_when_confirmed() -> None:
    async def scenario():
        FakeClient.code_needed = True
        flow = _flow()
        r = await flow.async_step_user(USER)
        assert r["type"] == "progress" and r["progress_action"] == "wait_code"
        await r["progress_task"]
        r = await flow.async_step_pair()
        assert r == {"type": "progress_done", "next_step_id": "confirm_code"}
        r = await flow.async_step_confirm_code()
        assert r["type"] == "form" and r["description_placeholders"] == {"code": "012345"}
        r = await flow.async_step_confirm_code({"matches": True})
        assert r["type"] == "progress" and r["progress_action"] == "finishing"
        await r["progress_task"]
        assert (await flow.async_step_finish())["next_step_id"] == "done"
        entry = await flow.async_step_done()
        flow.async_remove()
        return entry

    entry = _run(scenario())
    assert entry["type"] == "create_entry" and entry["data"]["address"] == "AA:BB:CC:DD:EE:FF"


def test_unticked_box_rejects_the_pairing_and_aborts() -> None:
    async def scenario():
        FakeClient.code_needed = True
        flow = _flow()
        r = await flow.async_step_user(USER)
        await r["progress_task"]
        await flow.async_step_pair()
        r = await flow.async_step_confirm_code({"matches": False})
        await r["progress_task"]
        await flow.async_step_finish()
        return await flow.async_step_done()

    assert _run(scenario()) == {"type": "abort", "reason": "code_mismatch"}


def test_already_bonded_needs_no_code() -> None:
    async def scenario():
        FakeClient.code_needed = False  # BlueZ never asks: the bond already exists
        flow = _flow()
        r = await flow.async_step_user(USER)
        await r["progress_task"]
        r = await flow.async_step_pair()
        assert r == {"type": "progress_done", "next_step_id": "finish"}
        r = await flow.async_step_finish()
        if r["type"] == "progress":
            await r["progress_task"]
            r = await flow.async_step_finish()
        assert r["next_step_id"] == "done"
        return await flow.async_step_done()

    assert _run(scenario())["type"] == "create_entry"


def test_connection_failure_aborts_with_cannot_pair() -> None:
    class Boom(FakeClient):
        async def connect(self):
            raise OSError("no adapter")

    async def scenario():
        cf.ble_support.build_client = lambda hass, address, on_disconnect=None: Boom()
        try:
            flow = _flow()
            r = await flow.async_step_user(USER)
            await r["progress_task"]
            await flow.async_step_pair()
            r = await flow.async_step_finish()
            if r["type"] == "progress":
                await r["progress_task"]
            return await flow.async_step_done()
        finally:
            cf.ble_support.build_client = lambda hass, address, on_disconnect=None: FakeClient()

    assert _run(scenario()) == {"type": "abort", "reason": "cannot_pair"}


def test_skip_pairing_registers_without_touching_bluetooth() -> None:
    async def scenario():
        flow = _flow()
        return await flow.async_step_user({**USER, "skip_pairing": True})

    r = _run(scenario())
    assert r["type"] == "create_entry" and r["title"] == "Watch"
