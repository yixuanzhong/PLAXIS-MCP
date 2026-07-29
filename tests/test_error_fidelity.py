"""Error fidelity and parameter-shape correctness (WP-A .. WP-I).

Every case here comes from a measured failure in the 2026-07-23 Caros CLI
smoke test against PLAXIS 2D V22, cross-read against the PLAXIS scripting
console transcripts captured during the same runs: the console knew exactly
what was wrong, and none of it reached the caller.
"""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plaxis_mcp.bridge import (
    IDENTITY_PROBE_TIMEOUT_SECONDS,
    BridgeSession,
    BridgeSessionConfig,
    WorkerOperationError,
)
from plaxis_mcp.server import (
    PlaxisRole,
    ServerConfig,
    _bridge_error_result,
    _connect_session,
    create_server,
)
from plaxis_mcp.worker_runtime import (
    CONNECT_ATTEMPTS,
    WorkerError,
    WorkerRuntime,
    connect_deadline,
    describe_value,
    object_path,
    render_path,
    resolve_path,
    sanitise_vendor_message,
)


class PlxScriptingError(Exception):
    """Shaped like plxscripting's own error: a header line, then the reason."""

    def __init__(self, extra_info):
        super(PlxScriptingError, self).__init__("Unsuccessful command:\n" + extra_info)


class FakeProperty:
    def __init__(self, value):
        self.value = value


class FakeReference:
    """A property whose value is another object, as PLAXIS's IPObject is."""

    def __init__(self, target):
        self.value = target


class FakeObject:
    """A stand-in for a plxscripting proxy: a guid plus property members."""

    def __init__(self, name, identification=None, type_name="Object", **members):
        self._guid = "{%s}" % name
        self.Name = FakeProperty(name)
        self.Identification = FakeProperty(identification if identification is not None else name)
        self.TypeName = FakeProperty(type_name)
        for key, value in members.items():
            setattr(self, key, value if hasattr(value, "value") else FakeProperty(value))


class FakePhase(FakeObject):
    def __init__(self, name, should_calculate=True, log="", result="OK", first=-1, last=-1):
        FakeObject.__init__(self, name, type_name="Phase")
        self.ShouldCalculate = FakeProperty(should_calculate)
        self.CalculationResult = FakeProperty(result)
        self.DeformCalcType = FakeProperty("Plastic")
        self.PorePresCalcType = FakeProperty("Phreatic")
        self.FirstStep = FakeProperty(first)
        self.LastStep = FakeProperty(last)
        self.LogInfo = FakeProperty(log)


class FakeServer:
    def __init__(self):
        self.active = True
        self.server_full_name = "PLAXIS 2D V22"

    def close(self):
        return None


def input_runtime(global_object):
    runtime = WorkerRuntime(
        role="input", new_server_factory=lambda **kwargs: (FakeServer(), global_object)
    )
    runtime.handle_request(
        {
            "protocol": 1,
            "id": 1,
            "operation": "connect",
            "params": {"host": "127.0.0.1", "port": 10000, "password": "pw", "timeout": 5},
        }
    )
    return runtime


def call(runtime, operation, **params):
    return runtime.handle_request(
        {
            "protocol": 1,
            "id": 9,
            "operation": "call",
            "params": {"method": operation, "params": params},
        }
    )


class VendorMessageSanitiserTests(unittest.TestCase):
    """WP-A. Suppressing vendor exceptions is right for tracebacks and wrong
    for PLAXIS's validation messages, which are API contract: they name the
    rejected property, the unknown parameter, the read-only member."""

    def test_plxscriptings_header_line_is_skipped_for_the_real_complaint(self):
        # plxscripting raises "Unsuccessful command:\n<what PLAXIS said>", so a
        # strict first-line rule would deliver only the header.
        self.assertEqual(
            sanitise_vendor_message(PlxScriptingError("Cannot set read-only property Top")),
            ("Cannot set read-only property Top", False),
        )

    def test_the_four_smoke_test_console_messages_survive_verbatim(self):
        for message in (
            "Cannot set read-only property Top",
            "Unknown property: EA",
            "Invalid parameters. Make sure that the specified parameters match the command.",
            "Text must be followed by space or operand",
        ):
            self.assertEqual(
                sanitise_vendor_message(PlxScriptingError(message)), (message, False)
            )

    def test_windows_unc_and_file_url_paths_are_stripped(self):
        for text, expected in (
            (
                r"Could not open C:\Users\someone\Documents\model.p2dx for writing",
                "Could not open [path] for writing",
            ),
            (
                r"Access denied on \\fileserver\share\projects\model.p2dx",
                "Access denied on [path]",
            ),
            ("Load failed: file:///C:/temp/model.p2dx is locked", "Load failed: [path] is locked"),
        ):
            self.assertEqual(sanitise_vendor_message(text), (expected, False))

    def test_paths_containing_spaces_do_not_leak_their_tail(self):
        """A pattern that stops at the first space is not a redaction.

        Windows directory names contain spaces constantly, and the old one
        turned C:\\Users\\someone\\My Projects\\model.p2dx into
        "[path] Projects\\model.p2dx" -- the drive went, the layout stayed.
        """

        for text, expected in (
            (
                r"Could not open C:\Users\someone\My Projects\model.p2dx for writing",
                "Could not open [path] for writing",
            ),
            (
                r'Could not open "C:\My Docs\model.p2dx" for writing',
                "Could not open [path] for writing",
            ),
            (
                "Access denied on " + "\\" * 2 + r"fileserver\share\My Docs\model.p2dx now",
                "Access denied on [path] now",
            ),
            (
                '"' + "\\" * 2 + r'fileserver\share\My Docs" is read-only',
                "[path] is read-only",
            ),
            (
                r"Saved to C:\Users\John Smith\Documents\model.p2dx",
                "Saved to [path]",
            ),
        ):
            message, _ = sanitise_vendor_message(text)
            self.assertEqual(message, expected)
            self.assertNotIn("\\", message)

    def test_a_second_path_does_not_swallow_the_prose_between_them(self):
        self.assertEqual(
            sanitise_vendor_message(r"Copy from C:\temp\a.txt to D:\other\b.txt failed"),
            ("Copy from [path] to [path] failed", False),
        )

    def test_a_message_that_is_only_a_path_yields_nothing(self):
        self.assertIsNone(sanitise_vendor_message(r"C:\Users\someone\model.p2dx"))

    def test_traceback_frames_are_dropped(self):
        text = (
            "Traceback (most recent call last):\n"
            '  File "C:\\plaxis\\plxscripting\\server.py", line 207, in handle\n'
            "    raise PlxScriptingError(...)\n"
            "Unknown property: EA"
        )
        self.assertEqual(sanitise_vendor_message(text), ("Unknown property: EA", False))

    def test_empty_blank_and_none_yield_nothing(self):
        for value in (None, "", "   ", "\n\n", "---", Exception("")):
            self.assertIsNone(sanitise_vendor_message(value))

    def test_non_ascii_is_preserved(self):
        self.assertEqual(
            sanitise_vendor_message("Ungültiger Parameter: γUnsat"),
            ("Ungültiger Parameter: γUnsat", False),
        )

    def test_truncation_is_reported_out_of_band_and_never_marked_in_the_text(self):
        message, truncated = sanitise_vendor_message("x" * 400)
        self.assertTrue(truncated)
        self.assertEqual(len(message), 300)
        self.assertNotIn("...", message)
        self.assertNotIn("…", message)


class VendorMessagePassThroughTests(unittest.TestCase):
    """WP-A end to end: what PLAXIS said reaches the client's error envelope."""

    def test_a_rejected_property_write_carries_plaxis_own_words(self):
        class Project:
            Title = FakeProperty("Demo")

            def __setattr__(self, name, value):
                raise PlxScriptingError("Cannot set read-only property Title")

        class Root:
            def __init__(self):
                self.Project = Project()

        result = call(input_runtime(Root()), "set_property", path="Project.Title", value="New")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "PLAXIS_ERROR")
        # The existing wording is unchanged, so nothing matching on it breaks.
        self.assertEqual(
            result["error"]["message"], "PLAXIS rejected the requested property update."
        )
        self.assertEqual(result["error"]["plaxis_message"], "Cannot set read-only property Title")

    def test_a_failed_command_carries_plaxis_own_words(self):
        class Root:
            def gotostages(self):
                return True

            def setcurrentphase(self, phase):
                raise PlxScriptingError("Invalid parameters. Make sure that the parameters match.")

            def __init__(self):
                self.Phase_1 = FakeObject("Phase_1", type_name="Phase")

        result = call(input_runtime(Root()), "set_current_phase", phase="Phase_1")

        self.assertEqual(result["error"]["message"], "PLAXIS could not execute 'setcurrentphase'.")
        self.assertEqual(
            result["error"]["plaxis_message"],
            "Invalid parameters. Make sure that the parameters match.",
        )

    def test_a_password_echoed_by_a_vendor_message_is_still_redacted(self):
        class Root:
            def gotostages(self):
                return True

            def setcurrentphase(self, phase):
                raise PlxScriptingError("Rejected request with code pw")

            def __init__(self):
                self.Phase_1 = FakeObject("Phase_1", type_name="Phase")

        result = call(input_runtime(Root()), "set_current_phase", phase="Phase_1")

        self.assertNotIn("pw", result["error"]["plaxis_message"])
        self.assertIn("[REDACTED]", result["error"]["plaxis_message"])

    def test_the_host_and_the_mcp_payload_forward_the_vendor_message(self):
        class Session:
            def status(self):
                return {"state": "connected", "connected": True}

            def invoke(self, operation, **params):
                raise WorkerOperationError(
                    "PLAXIS_ERROR",
                    "PLAXIS could not execute 'calculate'.",
                    state="connected",
                    plaxis_message="Phase_2 did not converge",
                    details={"phases": [{"name": "Phase_2", "status": "Failed"}]},
                )

        server = create_server(ServerConfig(role=PlaxisRole.INPUT), session_factory=Session)
        result = asyncio.run(server.call_tool("calculate", {}))

        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["plaxis_message"], "Phase_2 did not converge")
        self.assertEqual(
            result.structuredContent["details"]["phases"][0]["status"], "Failed"
        )


class ObjectPathTests(unittest.TestCase):
    """WP-B. Described objects now carry the exact string a reference
    parameter accepts -- the loop an agent could not close before, having fed
    a described object straight back and got a PLAXIS parse error."""

    def test_a_described_object_carries_a_path_that_resolves(self):
        class Root:
            def __init__(self):
                self.SoilMat_1 = FakeObject("SoilMat_1", identification="SZ")

        root = Root()
        described = describe_value(root.SoilMat_1)

        self.assertEqual(described["path"], "SoilMat_1")
        self.assertIs(resolve_path(root, described["path"]), root.SoilMat_1)

    def test_a_name_that_is_not_a_resolvable_token_gets_no_path(self):
        # Publishing a path that cannot be resolved would be worse than
        # publishing none: it invites a second failing call.
        for name in ("Text must be followed by space", "", None, "1_leading_digit"):
            renamed = FakeObject("placeholder")
            renamed.Name = FakeProperty(name)
            self.assertIsNone(object_path(renamed))
            self.assertNotIn("path", describe_value(renamed))

    def test_render_path_round_trips_indexed_paths(self):
        self.assertEqual(
            render_path(["Soillayers", 1, "Zones", 0, "Top"]), "Soillayers[1].Zones[0].Top"
        )


class ScalarValueTests(unittest.TestCase):
    """WP-B/WP-H. plxscripting builds every call as a command line: a mapping
    has no representation there and a sequence becomes a parenthesised group,
    so an object value was never something PLAXIS could receive."""

    def _root(self):
        class Root:
            def __init__(self):
                self.Project = FakeObject("Project", Title="Demo")
                self.Phase_1 = FakeObject("Phase_1", type_name="Phase")

            def dump(self, *args):
                return "dumped"

        return Root()

    def test_set_property_rejects_an_object_value_and_names_the_fix(self):
        result = call(
            input_runtime(self._root()),
            "set_property",
            path="Project.Title",
            value={"kind": "object", "name": "SoilMat_1"},
        )

        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
        self.assertIn("path", result["error"]["message"])
        self.assertIn("assign_material", result["error"]["message"])

    def test_set_property_still_accepts_every_scalar(self):
        runtime = input_runtime(self._root())
        for value in ("Site A", 3, 2.5, True, None):
            result = call(runtime, "set_property", path="Project.Title", value=value)
            self.assertTrue(result["ok"], result)

    def test_call_method_rejects_non_scalar_arguments(self):
        result = call(input_runtime(self._root()), "call_method", method="dump", args=[{"a": 1}])

        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
        self.assertIn("scalar", result["error"]["message"])

    def test_set_phase_property_rejects_non_scalar_values(self):
        result = call(
            input_runtime(self._root()),
            "set_phase_property",
            path="Phase_1",
            property="Deform",
            phase="Phase_1",
            value=[1, 2],
        )

        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")


class SchemaBoundaryTests(unittest.TestCase):
    """WP-B/WP-H acceptance: the advertised schema and the worker's contract
    say the same thing, so a non-scalar is refused before it can travel."""

    class RecordingSession:
        def __init__(self):
            self.calls = []

        def status(self):
            return {"state": "connected", "connected": True}

        def invoke(self, operation, **params):
            self.calls.append((operation, params))
            return {"ok": True}

    def _server(self):
        session = self.RecordingSession()
        return create_server(
            ServerConfig(role=PlaxisRole.INPUT), session_factory=lambda: session
        ), session

    def _schema(self, name):
        server, _ = self._server()
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        return tools[name].inputSchema

    def test_set_property_advertises_scalars_only(self):
        types = {
            option.get("type")
            for option in self._schema("set_property")["properties"]["value"]["anyOf"]
        }

        self.assertEqual(types, {"string", "integer", "number", "boolean", "null"})

    def test_call_method_advertises_scalar_arguments_only(self):
        items = self._schema("call_method")["properties"]["args"]["anyOf"][0]["items"]

        self.assertEqual(
            {option.get("type") for option in items["anyOf"]},
            {"string", "integer", "number", "boolean", "null"},
        )

    def test_an_object_value_never_reaches_the_worker(self):
        server, session = self._server()

        with self.assertRaises(Exception):
            asyncio.run(server.call_tool("set_property", {"path": "X", "value": {"a": 1}}))
        self.assertEqual(session.calls, [])

    def test_the_new_read_tools_are_annotated_read_only(self):
        server, _ = self._server()
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

        for name in ("list_objects", "model_state"):
            self.assertTrue(tools[name].annotations.readOnlyHint, name)
            self.assertFalse(tools[name].annotations.destructiveHint, name)


class MaterialReferenceRoutingTests(unittest.TestCase):
    """WP-C. 37 retries in one run went into set_property on Plate_1.Material;
    the rejection pointed at a longer path when what was needed was a
    different tool."""

    def _runtime(self):
        class Root:
            def __init__(self):
                self.Plate_1 = FakeObject("Plate_1", type_name="Plate")
                self.Plate_1.Material = FakeReference(FakeObject("PlateMat_1"))

        return input_runtime(Root())

    def test_setting_a_material_names_assign_material_with_a_usable_call(self):
        result = call(self._runtime(), "set_property", path="Plate_1.Material", value="PlateMat_1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "WRONG_TOOL")
        self.assertIn('assign_material(paths=["Plate_1"]', result["error"]["message"])

    def test_the_rule_generalises_to_a_nested_owner(self):
        result = call(
            self._runtime(), "set_property", path="Soillayer_1.Soil.Material", value="SoilMat_1"
        )

        self.assertIn('assign_material(paths=["Soillayer_1.Soil"]', result["error"]["message"])


class DerivedPropertyTests(unittest.TestCase):
    """WP-D. The worst single failure of the smoke test: ten minutes of
    top_manual / setproperties / parenthesised-form guessing at a write PLAXIS
    was never going to accept, and the run was killed with no progress."""

    class Zone:
        def __init__(self):
            self.Top = FakeProperty(0.0)
            self.Bottom = FakeProperty(-5.0)
            self.writes = []

        def __setattr__(self, name, value):
            if name != "writes" and "writes" in self.__dict__:
                self.__dict__["writes"].append(name)
            object.__setattr__(self, name, value)

    def _runtime(self):
        zone = self.Zone()
        layer = FakeObject("Soillayer_2", type_name="Soillayer")
        layer.Zones = [zone]

        class Root:
            def __init__(self):
                self.Soillayer_2 = layer

        return input_runtime(Root()), zone

    def test_a_zone_top_write_is_refused_before_plaxis_is_touched(self):
        runtime, zone = self._runtime()
        result = call(runtime, "set_property", path="Soillayer_2.Zones[0].Top", value=-1.0)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "READ_ONLY_PROPERTY")
        self.assertIn("Bottom", result["error"]["message"])
        self.assertIn("top_manual", result["error"]["message"])
        self.assertEqual(zone.writes, [])

    def test_the_indexed_spelling_of_a_lower_layer_is_refused_too(self):
        zone = self.Zone()
        layers = [FakeObject("Soillayer_1"), FakeObject("Soillayer_2")]
        layers[1].Zones = [zone]

        class Root:
            def __init__(self):
                self.Soillayers = layers

        result = call(
            input_runtime(Root()), "set_property", path="Soillayers[1].Zones[0].Top", value=-1.0
        )

        self.assertEqual(result["error"]["code"], "READ_ONLY_PROPERTY")

    def test_the_topmost_layer_is_not_blocked_by_the_rule(self):
        """Only the layers below the top derive their Top. Refusing the
        topmost one would trade the measured thrash for a blocked mutation --
        so a path that cannot prove which layer it names goes to PLAXIS, whose
        answer is now legible on its own."""

        zone = self.Zone()
        top_layer = FakeObject("Soillayer_1")
        top_layer.Zones = [zone]

        class Root:
            def __init__(self):
                self.Soillayer_1 = top_layer
                self.Soillayers = [top_layer]

        runtime = input_runtime(Root())
        for path in ("Soillayer_1.Zones[0].Top", "Soillayers[0].Zones[0].Top"):
            result = call(runtime, "set_property", path=path, value=-1.0)
            self.assertTrue(result["ok"], (path, result))
        self.assertEqual(zone.writes, ["Top", "Top"])


class LayerLevelZonePropertyTests(unittest.TestCase):
    """A soil layer's Top/Bottom/Thickness are per-zone lists, so a layer-level
    write raises PLAXIS's "'PlxObjectPropertyList' object has no attribute
    '__set__'" -- an internal type name and no way forward. Measured through
    Caros: eleven calls permuting the four properties across two layers and two
    tools, ending in a project reset that crashed PLAXIS."""

    class Zone:
        def __init__(self):
            self.Top = FakeProperty(0.0)
            self.Bottom = FakeProperty(-5.0)

    def _runtime(self):
        layer = FakeObject("Soillayer_1", type_name="Soillayer")
        # The live shape: the layer-level member is a list, one entry per zone.
        layer.Top = FakeProperty([0.0])
        layer.Bottom = FakeProperty([-1.0])
        layer.Thickness = FakeProperty([1.0])
        layer.top_manual = FakeProperty([0.0])
        layer.Zones = [self.Zone()]

        class Root:
            def __init__(self):
                self.Soillayer_1 = layer
                self.Soillayers = [layer]

        return input_runtime(Root())

    def test_a_layer_level_write_names_the_zone_path_that_works(self):
        result = call(self._runtime(), "set_property", path="Soillayer_1.Top", value=0)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_PATH")
        self.assertIn("Soillayer_1.Zones[0].Top", result["error"]["message"])
        self.assertIn("per-zone list", result["error"]["message"])

    def test_every_per_zone_property_the_agent_tried_is_covered(self):
        """Top, Bottom, Thickness and top_manual all failed identically live;
        catching only Top would just move the thrash one property along."""

        runtime = self._runtime()
        for prop in ("Top", "Bottom", "Thickness", "top_manual"):
            result = call(runtime, "set_property", path="Soillayer_%s.%s" % (1, prop), value=1)
            self.assertEqual(result["error"]["code"], "INVALID_PATH", prop)
            self.assertIn("Soillayer_1.Zones[0].%s" % prop, result["error"]["message"])

    def test_the_indexed_layer_spelling_is_covered_too(self):
        result = call(self._runtime(), "set_property", path="Soillayers[0].Thickness", value=5)

        self.assertEqual(result["error"]["code"], "INVALID_PATH")
        self.assertIn("Soillayers[0].Zones[0].Thickness", result["error"]["message"])

    def test_the_method_spelling_of_the_same_write_is_caught(self):
        """After set_property refused it, the agent moved to call_method."""

        runtime = self._runtime()
        result = call(
            runtime, "call_method", path="Soillayer_1", method="set", args=["Bottom", -5]
        )

        self.assertFalse(result["ok"])
        self.assertIn("Soillayer_1.Zones[0].Bottom", result["error"]["message"])

    def test_calling_a_method_on_the_property_list_is_caught(self):
        result = call(self._runtime(), "call_method", path="Soillayer_1.Top", method="set", args=[0])

        self.assertFalse(result["ok"])
        self.assertIn("Soillayer_1.Zones[0].Top", result["error"]["message"])

    def test_a_zone_level_write_still_goes_through(self):
        """The guard must not swallow the spelling it is recommending."""

        runtime = self._runtime()
        result = call(runtime, "set_property", path="Soillayer_1.Zones[0].Top", value=-1.0)

        self.assertTrue(result["ok"], result)

    def test_unrelated_properties_and_objects_are_untouched(self):
        runtime = self._runtime()

        self.assertTrue(call(runtime, "set_property", path="Soillayer_1.Name", value="x")["ok"])
        self.assertIsNone(
            WorkerRuntime._per_zone_property_message(["Plate_1", "Top"]),
            "only a provable soil layer may trigger the rule",
        )

    def test_an_unprovable_layer_spelling_still_reaches_plaxis(self):
        """Same contract as the derived-Top rule: what the path cannot prove
        goes to PLAXIS, whose own answer is legible."""

        self.assertIsNone(WorkerRuntime._per_zone_property_message(["SomeLayer", "Top"]))
        self.assertIsNone(WorkerRuntime._per_zone_property_message(["Borehole_1", "Top"]))

    def test_a_writable_neighbour_property_is_untouched_by_the_rule(self):
        """Bottom on a zone is the write the message recommends; it must work."""

        runtime = self._runtime()
        result = call(runtime, "set_property", path="Soillayer_1.Zones[0].Bottom", value=-6.0)

        self.assertTrue(result["ok"], result)


class MaterialPropertyCatalogTests(unittest.TestCase):
    """WP-E. An unknown name used to surface only as PLAXIS's "Unknown
    property: EA", and the observed recovery was to invent deep paths such as
    AnchorMat_1.Internal.Mechanical.Properties.Stiffness, none of which
    exist."""

    class Root:
        def __init__(self):
            self.calls = []

        def soilmat(self, *args):
            self.calls.append(("soilmat", args))
            return FakeObject("SoilMat_1")

        def anchormat(self, *args):
            self.calls.append(("anchormat", args))
            return FakeObject("AnchorMat_1")

    def test_an_unknown_name_is_rejected_before_plaxis_sees_it(self):
        root = self.Root()
        result = call(
            input_runtime(root), "create_material", kind="soil", properties={"EA": 1000.0}
        )

        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
        self.assertIn("Unknown property 'EA'", result["error"]["message"])
        self.assertIn("gammaUnsat", result["error"]["message"])
        self.assertEqual(root.calls, [])

    def test_the_rejection_names_the_way_through_for_another_plaxis_version(self):
        result = call(
            input_runtime(self.Root()), "create_material", kind="soil", properties={"Nonesuch": 1}
        )

        self.assertIn("set_property", result["error"]["message"])

    def test_a_case_mismatch_is_named_as_a_case_mismatch(self):
        result = call(
            input_runtime(self.Root()),
            "create_material",
            kind="soil",
            properties={"gammaunsat": 17.0},
        )

        self.assertIn("case-sensitive", result["error"]["message"])
        self.assertIn("gammaUnsat", result["error"]["message"])

    def test_a_name_the_catalog_lacks_is_checked_against_the_model_first(self):
        """The catalog is an accept-list, not an oracle. PLAXIS accepts
        InterfaceStrengthDetermination where its own object reference says
        InterfaceStrengthMethod, and a soil material's members change with its
        constitutive model -- so a static list will always lag. Verified live:
        the doc-derived list rejected nine properties PLAXIS accepts."""

        root = self.Root()
        existing = FakeObject("SZ", identification="SZ", type_name="SoilMat")
        existing.SomeNewPlaxisProperty = FakeProperty(1)
        root.Materials = [existing]

        result = call(
            input_runtime(root),
            "create_material",
            kind="soil",
            properties={"SomeNewPlaxisProperty": 5},
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(root.calls, [("soilmat", ("SomeNewPlaxisProperty", 5))])

    def test_a_name_on_neither_the_catalog_nor_the_model_is_still_refused(self):
        root = self.Root()
        root.Materials = [FakeObject("SZ", identification="SZ", type_name="SoilMat")]

        result = call(
            input_runtime(root), "create_material", kind="soil", properties={"Nonesuch": 5}
        )

        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
        self.assertIn("not on an existing SoilMat in this model", result["error"]["message"])
        self.assertEqual(root.calls, [])

    def test_the_live_spellings_the_object_reference_omits_are_accepted(self):
        root = self.Root()
        result = call(
            input_runtime(root),
            "create_material",
            kind="soil",
            properties={
                "InterfaceStrengthDetermination": "Manual",
                "Rinter": 0.67,
                "K0Primary": 0.5,
                "PermHorizontalPrimary": 0.0,
            },
        )

        self.assertTrue(result["ok"], result)

    def test_the_same_name_can_be_valid_for_one_kind_and_not_another(self):
        root = self.Root()
        runtime = input_runtime(root)

        accepted = call(runtime, "create_material", kind="anchor", properties={"EA": 1000.0})
        rejected = call(runtime, "create_material", kind="soil", properties={"EA": 1000.0})

        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(root.calls, [("anchormat", ("EA", 1000.0))])
        self.assertFalse(rejected["ok"])


class CalculateOutcomeTests(unittest.TestCase):
    """WP-F. The one blocker the smoke test could not diagnose from the MCP
    side at all: calculate failed and reported nothing but "could not
    execute", while ShouldCalculate, CalculationType and LogInfo all read
    normal."""

    class Root:
        def __init__(self, fail=False):
            self.Phases = [
                FakePhase("InitialPhase", should_calculate=False, result="OK", first=0, last=0),
                FakePhase("Phase_1", log="Soil body collapses", result="Failed"),
            ]
            self._fail = fail

        def gotostages(self, *args):
            return True

        def calculate(self, *phases):
            if self._fail:
                raise PlxScriptingError("Soil body collapses")
            return True

    def test_a_successful_calculate_returns_the_per_phase_table(self):
        result = call(input_runtime(self.Root()), "calculate")

        self.assertTrue(result["ok"], result)
        outcomes = result["result"]["phase_outcomes"]
        self.assertEqual([phase["name"] for phase in outcomes], ["InitialPhase", "Phase_1"])
        self.assertEqual(outcomes[1]["status"], "Failed")
        self.assertEqual(outcomes[1]["log"], "Soil body collapses")
        self.assertEqual(outcomes[1]["path"], "Phase_1")

    def test_a_failing_calculate_names_the_phase_and_carries_the_log(self):
        result = call(input_runtime(self.Root(fail=True)), "calculate")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["plaxis_message"], "Soil body collapses")
        failed = [
            phase for phase in result["error"]["details"]["phases"] if phase["status"] == "Failed"
        ]
        self.assertEqual(failed[0]["name"], "Phase_1")
        self.assertEqual(failed[0]["log"], "Soil body collapses")

    def test_each_phase_outcome_names_its_status_code(self):
        """0/1/2 = not_calculated/calculated/failed, read off a live calculate
        whose own summary named which phases did what."""

        root = self.Root()
        root.Phases[0].CalculationResult = FakeProperty(1)   # InitialPhase calculated
        root.Phases[1].CalculationResult = FakeProperty(2)   # Phase_1 failed
        result = call(input_runtime(root), "calculate")

        outcomes = result["result"]["phase_outcomes"]
        self.assertEqual(outcomes[0]["status_text"], "calculated")
        self.assertEqual(outcomes[1]["status_text"], "failed")

    def test_an_unrecognised_status_code_stays_unnamed_with_the_value_kept(self):
        root = self.Root()
        root.Phases[0].CalculationResult = FakeProperty(7)
        outcomes = call(input_runtime(root), "calculate")["result"]["phase_outcomes"]

        self.assertEqual(outcomes[0]["status"], 7)
        self.assertIsNone(outcomes[0]["status_text"])

    def test_a_member_this_plaxis_does_not_expose_is_reported_as_absent(self):
        root = self.Root()
        del root.Phases[1].LogInfo

        outcomes = call(input_runtime(root), "calculate")["result"]["phase_outcomes"]

        self.assertIsNone(outcomes[1]["log"])
        self.assertIn("LogInfo", outcomes[1]["unavailable_members"])


class ModelStateTests(unittest.TestCase):
    """WP-G. Nothing let an agent ask "is this model calculable?" before
    spending a run finding out."""

    class MeshStatus:
        def __init__(self, elements, nodes, status="OK"):
            self.Status = FakeProperty(status)
            self.NumberOfElements = FakeProperty(elements)
            self.NumberOfNodes = FakeProperty(nodes)

    def _root(self, elements=240, material=True, phases=2, model=True, status="Up to date",
              placeholder=False):
        root = type("Root", (object,), {})()
        if model:
            model_object = FakeObject("Model")
            model_object.MeshStatus = ModelStateTests.MeshStatus(
                elements, elements * 8, status=status
            )
            model_object.MeshFeedback = FakeProperty("")
            root.Model = model_object
        root.Phases = [FakePhase("InitialPhase", should_calculate=False)] + [
            FakePhase("Phase_%d" % index) for index in range(1, phases)
        ]
        plate = FakeObject("Plate_1", type_name="Plate")
        if not material:
            plate.Material = FakeProperty(None)
        elif placeholder:
            # A material created with no properties: PLAXIS leaves its
            # identification as the placeholder token and rejects it at
            # calculate.
            plate.Material = FakeReference(
                FakeObject("PlateMat_2", identification="<NoName>")
            )
        else:
            plate.Material = FakeReference(FakeObject("PlateMat_1", identification="Deck"))
        root.Plates = [plate]
        return root

    def test_a_calculable_model_reports_nothing_blocking(self):
        result = call(input_runtime(self._root()), "model_state")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["blocking"], [])
        self.assertEqual(result["result"]["unknown"], [])
        self.assertTrue(result["result"]["calculable"])
        self.assertEqual(result["result"]["mesh"]["element_count"], 240)

    def test_an_unmeshed_model_names_the_missing_mesh(self):
        result = call(input_runtime(self._root(elements=0)), "model_state")

        self.assertIn("generate_mesh", " ".join(result["result"]["blocking"]))
        self.assertFalse(result["result"]["calculable"])

    def test_a_stale_mesh_is_blocking_even_with_elements_present(self):
        """An element count only proves a mesh was generated once. Reading it
        alone reported a model whose mesh no longer matched its geometry as
        calculable, with nothing blocking and nothing unknown."""

        result = call(
            input_runtime(self._root(elements=240, status="Needs regeneration")), "model_state"
        )

        self.assertEqual(result["result"]["mesh"]["staleness"], "stale")
        self.assertIn("regenerate", " ".join(result["result"]["blocking"]).lower())
        self.assertFalse(result["result"]["calculable"])

    def test_an_unrecognised_mesh_status_is_unknown_not_healthy(self):
        result = call(input_runtime(self._root(status="Zustand 7")), "model_state")

        self.assertEqual(result["result"]["mesh"]["staleness"], "unrecognised")
        self.assertEqual(result["result"]["blocking"], [])
        self.assertIn("does not recognise", " ".join(result["result"]["unknown"]))
        self.assertIsNone(result["result"]["calculable"])

    def test_the_known_ready_statuses_read_as_ready(self):
        runtime = input_runtime(self._root())
        for status in ("Up to date", "OK", "Mesh generated", "Valid"):
            state = call(input_runtime(self._root(status=status)), "model_state")["result"]
            self.assertEqual(state["mesh"]["staleness"], "ready", status)
            self.assertTrue(state["calculable"], status)
        self.assertTrue(call(runtime, "model_state")["ok"])

    def test_an_enum_status_is_a_standing_caveat_not_a_verdict(self):
        """PLAXIS 2D V22 reports its mesh status as an undocumented code, and
        live testing showed that code frozen at 1 through a moved geometry
        point and a soil-layer edit -- with NumberOfElements frozen too. So
        mesh currency is not readable there, and saying so is the only honest
        option: not a fault (blocking), not a check that failed to run
        (unknown), but a limit stated every time."""

        state = call(input_runtime(self._root(status=1)), "model_state")["result"]

        self.assertEqual(state["mesh"]["staleness"], "not_available")
        self.assertEqual(state["mesh"]["status"], 1)
        self.assertEqual(state["blocking"], [])
        self.assertEqual(state["unknown"], [])
        self.assertTrue(state["calculable"])
        self.assertIn("Regenerate the mesh", " ".join(state["caveats"]))

    def test_a_textual_stale_status_still_blocks(self):
        state = call(
            input_runtime(self._root(status="Needs regeneration")), "model_state"
        )["result"]

        self.assertEqual(state["caveats"], [])
        self.assertIn("regenerate", " ".join(state["blocking"]).lower())

    def test_a_feature_without_a_material_is_named(self):
        result = call(input_runtime(self._root(material=False)), "model_state")

        self.assertIn("Plate_1", " ".join(result["result"]["blocking"]))
        self.assertEqual(result["result"]["unassigned_materials"]["count"], 1)

    def test_a_placeholder_material_is_blocking_and_named(self):
        """The closed calculate blocker was a material created with no
        properties -- PLAXIS answered `Invalid material "<NoName> [PlateMat_2]"`.
        model_state now catches it before the wasted calculate, which an
        unassigned-materials check cannot: the material is present, just
        unconfigured."""

        result = call(input_runtime(self._root(placeholder=True)), "model_state")["result"]

        self.assertEqual(result["unassigned_materials"]["count"], 0)
        self.assertEqual(result["unassigned_materials"]["placeholder_count"], 1)
        self.assertEqual(
            result["unassigned_materials"]["placeholder_features"][0]["material"], "PlateMat_2"
        )
        joined = " ".join(result["blocking"])
        self.assertIn("Plate_1->PlateMat_2", joined)
        self.assertIn("never configured", joined)
        self.assertFalse(result["calculable"])

    def test_a_configured_material_is_not_flagged_as_placeholder(self):
        result = call(input_runtime(self._root()), "model_state")["result"]

        self.assertEqual(result["unassigned_materials"]["placeholder_count"], 0)
        self.assertTrue(result["calculable"])

    def test_a_model_with_only_the_initial_phase_is_named(self):
        result = call(input_runtime(self._root(phases=1)), "model_state")

        self.assertIn("initial phase", " ".join(result["result"]["blocking"]))

    def test_an_uncheckable_endpoint_says_unknown_rather_than_clear(self):
        result = call(input_runtime(self._root(model=False)), "model_state")

        self.assertFalse(result["result"]["mesh"]["available"])
        self.assertTrue(result["result"]["unknown"])
        # "Nothing is blocking" and "nothing could be checked" are different
        # answers, and an agent acts differently on each.
        self.assertIsNone(result["result"]["calculable"])


class ListObjectsTests(unittest.TestCase):
    """WP-H. There were listings for phases and materials and nothing else, so
    an agent looking for a soil cluster fell back to call_method dump -- 58
    such calls in one run, and the source of nearly every malformed command in
    the console transcripts."""

    def _root(self):
        root = type("Root", (object,), {})()
        root.Points = [FakeObject("Point_%d" % index, x=float(index), y=0.0) for index in range(3)]
        soil = FakeObject("Soil_1", type_name="Soil")
        soil.Material = FakeReference(FakeObject("SoilMat_1", identification="SZ"))
        polygon = FakeObject("Polygon_1", type_name="Polygon")
        polygon.x = FakeProperty([0.0, 15.0, 15.0, 0.0])
        polygon.y = FakeProperty([6.0, 6.0, 8.0, 8.0])
        soil.Parent = FakeReference(polygon)
        root.Soils = [soil]
        root.NodeToNodeAnchors = [FakeObject("Anchor_1", type_name="NodeToNodeAnchor")]
        # PLAXIS's global object resolves an object by its own name as well as
        # through the collection it belongs to, which is what makes a returned
        # `path` usable; the fake has to do both or it cannot test the loop.
        for item in root.Points + root.Soils + root.NodeToNodeAnchors:
            setattr(root, item.Name.value, item)
        return root

    def test_every_listed_object_carries_the_path_to_pass_back(self):
        root = self._root()
        result = call(input_runtime(root), "list_objects", kind="point")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["count"], 3)
        paths = [item["path"] for item in result["result"]["objects"]]
        self.assertEqual(paths, ["Point_0", "Point_1", "Point_2"])
        for path, point in zip(paths, root.Points):
            self.assertIs(resolve_path(root, path), point)

    def test_a_geometry_summary_replaces_the_dump_sweep(self):
        result = call(input_runtime(self._root()), "list_objects", kind="point")

        self.assertEqual(result["result"]["objects"][1]["geometry"], {"x": 1.0, "y": 0.0})

    def test_a_feature_reports_its_material_as_a_path(self):
        result = call(input_runtime(self._root()), "list_objects", kind="soil")

        references = result["result"]["objects"][0]["references"]
        self.assertEqual(references["Material"]["path"], "SoilMat_1")
        self.assertEqual(references["Material"]["identification"], "SZ")

    def test_a_soil_carries_its_parent_polygons_coordinates_and_bounds(self):
        """The WP-H acceptance case is "find the soil cluster at x=0-15,
        y=6-8". A soil has no coordinates of its own, so without the parent's
        geometry inline that answer still needs a lookup per candidate -- the
        dump sweep this tool exists to replace."""

        result = call(input_runtime(self._root()), "list_objects", kind="soil")

        parent = result["result"]["objects"][0]["references"]["Parent"]
        self.assertEqual(parent["path"], "Polygon_1")
        self.assertEqual(parent["geometry"]["x"], [0.0, 15.0, 15.0, 0.0])
        self.assertEqual(parent["geometry"]["x_range"], [0.0, 15.0])
        self.assertEqual(parent["geometry"]["y_range"], [6.0, 8.0])

    def test_a_cut_object_reports_its_bounding_box_as_numbers(self):
        """In staged and mesh mode every collection yields CutObjects, which
        expose no x/y at all -- BoundingBox is the only geometry there, and
        PLAXIS renders it as one formatted string (verified live)."""

        root = self._root()
        cut = FakeObject("Soil_1_Soil_2_1", type_name="StagedFeature")
        cut.BoundingBox = FakeProperty("min: (0.00; 6.00; 0.00) max: (12.00; 8.00; 0.00)")
        cut.Area = FakeProperty(24)
        root.Soils = [cut]

        geometry = call(input_runtime(root), "list_objects", kind="soil")["result"]["objects"][0][
            "geometry"
        ]

        self.assertEqual(geometry["Area"], 24)
        self.assertEqual(geometry["bounds"]["x"], [0.0, 12.0])
        self.assertEqual(geometry["bounds"]["y"], [6.0, 8.0])
        # The vendor's own string stays beside the parsed numbers.
        self.assertIn("min:", geometry["BoundingBox"])

    def test_an_unparseable_bounding_box_keeps_the_raw_value_and_adds_nothing(self):
        root = self._root()
        cut = FakeObject("Soil_1_Soil_2_1", type_name="StagedFeature")
        cut.BoundingBox = FakeProperty("not a bounding box")
        root.Soils = [cut]

        geometry = call(input_runtime(root), "list_objects", kind="soil")["result"]["objects"][0][
            "geometry"
        ]

        self.assertEqual(geometry["BoundingBox"], "not a bounding box")
        self.assertNotIn("bounds", geometry)

    def test_pagination_is_lossless_and_states_what_is_left(self):
        runtime = input_runtime(self._root())
        first = call(runtime, "list_objects", kind="point", limit=2)["result"]
        second = call(runtime, "list_objects", kind="point", offset=2, limit=2)["result"]

        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 2)
        self.assertEqual(second["returned_count"], 1)
        self.assertFalse(second["has_more"])

    def test_a_partially_present_kind_names_the_collection_it_could_not_read(self):
        result = call(input_runtime(self._root()), "list_objects", kind="anchor")

        self.assertEqual(result["result"]["collections"], ["NodeToNodeAnchors"])
        self.assertEqual(result["result"]["unavailable_collections"], ["FixedEndAnchors"])

    def test_a_kind_with_no_collection_at_all_fails_with_the_reason(self):
        result = call(input_runtime(self._root()), "list_objects", kind="plate")

        self.assertFalse(result["ok"])
        self.assertIn("Plates", result["error"]["message"])


class StalePortCorrectionTests(unittest.TestCase):
    """A pinned port that PLAXIS has moved away from failed every connect, and
    where another local service had taken the port, every attempt re-sent the
    password to it before the peer was identified. The pin is corrected to the
    port a running PLAXIS of this role actually owns, found in the OS process
    table rather than by probing, so discovery itself connects to nothing."""

    class _Session:
        """A bridge-shaped session that only accepts one port.

        ``names`` is what each port answers to an identity probe, mirroring the
        live V22 reading where one Input process held both the scripting port
        and an unrelated websocket port.
        """

        def __init__(self, working_port, error, names=None):
            self.config = BridgeSessionConfig(
                worker_python="python", role="input", port=10000
            )
            self._working_port = working_port
            self._error = error
            self._names = (
                names
                if names is not None
                else {working_port: "PLAXIS 2D Input/22.2.0.1078"}
            )
            self.attempts = []
            self.identified = []

        def invoke(self, operation, **params):
            return None

        def identify(self, port):
            self.identified.append(port)
            return {"port": port, "active": True, "server_full_name": self._names.get(port)}

        def connect(self):
            self.attempts.append(self.config.port)
            if self.config.port != self._working_port:
                raise self._error
            return {"connected": True, "state": "connected"}

    def _config(self, root, profile_path=None):
        return ServerConfig(
            role=PlaxisRole.INPUT,
            port=10000,
            installation_root=str(root),
            profile_path=None if profile_path is None else str(profile_path),
        )

    def _dead_port(self):
        return WorkerOperationError(
            "CONNECTION_FAILED", "Nothing is listening", retryable=False
        )

    def test_the_pin_moves_to_the_port_plaxis_actually_owns(self):
        session = self._Session(10007, self._dead_port())
        config = self._config("C:/plaxis")

        with patch("plaxis_mcp.server.discover_role_ports", return_value=[10007]):
            result = _connect_session(session, config)

        self.assertEqual(session.attempts, [10000, 10007])
        self.assertEqual(result["port_correction"]["previous_port"], 10000)
        self.assertEqual(result["port_correction"]["port"], 10007)
        # The corrected port has to be what status reports, not the stale pin.
        self.assertEqual(config.port, 10007)

    def test_a_squatted_port_reports_the_password_as_exposed(self):
        wrong_endpoint = WorkerOperationError(
            "WRONG_ENDPOINT", "not a PLAXIS scripting server", retryable=False
        )
        session = self._Session(10007, wrong_endpoint)

        with patch("plaxis_mcp.server.discover_role_ports", return_value=[10007]):
            result = _connect_session(session, self._config("C:/plaxis"))

        correction = result["port_correction"]
        self.assertTrue(correction["credential_exposed"])
        self.assertIn("rotate it", correction["detail"])

    def test_a_dead_port_correction_claims_no_exposure(self):
        session = self._Session(10007, self._dead_port())

        with patch("plaxis_mcp.server.discover_role_ports", return_value=[10007]):
            result = _connect_session(session, self._config("C:/plaxis"))

        self.assertFalse(result["port_correction"]["credential_exposed"])
        self.assertNotIn("exposed", result["port_correction"]["detail"])

    def test_the_identity_probe_does_not_inherit_the_full_connect_budget(self):
        """connect_timeout buys patience for a PLAXIS that is busy after
        new_project. Identifying a peer needs none of it, and spending that
        budget twice would make a silent port hang the session far longer than
        the connect it precedes."""

        session = BridgeSession(
            BridgeSessionConfig(
                worker_python="python", role="input", port=10000, connect_timeout=30.0
            )
        )

        with patch.object(session, "_bridge") as bridge:
            bridge.request.return_value = {"server_full_name": "PLAXIS 2D Input"}
            session.identify(10000)

        self.assertEqual(
            bridge.request.call_args[0][1]["timeout"], IDENTITY_PROBE_TIMEOUT_SECONDS
        )

    def test_a_squatted_pinned_port_never_receives_the_password(self):
        """The disclosure the post-connect check could only report after the
        fact is refused before the handshake instead."""

        session = self._Session(
            10003,
            self._dead_port(),
            names={10000: "Qualys-Correlation", 10003: "PLAXIS 2D Input/22.2.0.1078"},
        )

        with patch("plaxis_mcp.server.discover_role_ports", return_value=[10003]):
            result = _connect_session(session, self._config("C:/plaxis"))

        # 10000 was identified but never connected to.
        self.assertNotIn(10000, session.attempts)
        self.assertEqual(result["port_correction"]["port"], 10003)

    def test_a_refusal_before_the_handshake_claims_no_disclosure(self):
        """Rotating a password nobody received is wasted alarm."""

        session = self._Session(
            10003,
            self._dead_port(),
            names={10000: "Qualys-Correlation", 10003: "PLAXIS 2D Input/22.2.0.1078"},
        )

        with patch("plaxis_mcp.server.discover_role_ports", return_value=[10003]):
            result = _connect_session(session, self._config("C:/plaxis"))

        correction = result["port_correction"]
        self.assertFalse(correction["credential_exposed"])
        self.assertNotIn("exposed", correction["detail"])

    def test_an_unidentifiable_pinned_port_is_still_connected_to(self):
        """A busy PLAXIS cannot answer a probe either. Treating silence as a
        mismatch would defeat the connect retry that WP-I added."""

        session = self._Session(10000, self._dead_port(), names={})

        with patch("plaxis_mcp.server.discover_role_ports") as discover:
            result = _connect_session(session, self._config("C:/plaxis"))

        self.assertEqual(session.attempts, [10000])
        self.assertTrue(result["connected"])
        discover.assert_not_called()

    def test_a_build_that_omits_the_role_word_still_connects(self):
        """The pinned-port check rejects only an explicit contradiction; a name
        without the role word is not proof of a mismatch."""

        session = self._Session(10000, self._dead_port(), names={10000: "PLAXIS 2D V22"})

        result = _connect_session(session, self._config("C:/plaxis"))

        self.assertEqual(session.attempts, [10000])
        self.assertTrue(result["connected"])

    def test_the_other_roles_endpoint_is_refused_without_sending_the_password(self):
        session = self._Session(
            10000, self._dead_port(), names={10000: "PLAXIS 2D Output/22.2.0.1078"}
        )

        with patch("plaxis_mcp.server.discover_role_ports", return_value=[]):
            with self.assertRaises(WorkerOperationError) as raised:
                _connect_session(session, self._config("C:/plaxis"))

        self.assertEqual(session.attempts, [])
        self.assertEqual(raised.exception.code, "WRONG_ENDPOINT")
        self.assertIn("No password was sent", str(raised.exception))

    def test_a_correction_is_still_reported_when_the_retry_fails(self):
        """Live on V22: the pin moved 10000 -> 10003 and the reconnect then
        failed on a stale password. The raised error has to say the profile
        changed, or the operator is left with a silently rewritten pin."""

        class _AlwaysFails(self._Session):
            def connect(self):
                self.attempts.append(self.config.port)
                if self.config.port == 10000:
                    raise WorkerOperationError(
                        "CONNECTION_FAILED", "Nothing is listening", retryable=False
                    )
                raise WorkerOperationError(
                    "AUTHENTICATION_FAILED", "rejected the stored password"
                )

        session = _AlwaysFails(10003, self._dead_port())

        with patch("plaxis_mcp.server.discover_role_ports", return_value=[10003]):
            with self.assertRaises(WorkerOperationError) as raised:
                _connect_session(session, self._config("C:/plaxis"))

        message = str(raised.exception)
        self.assertIn("rejected the stored password", message)
        self.assertIn("repinned", message)
        self.assertIn("10003", message)
        self.assertEqual(raised.exception.code, "AUTHENTICATION_FAILED")

    def test_a_busy_plaxis_keeps_its_port(self):
        """Retryable means the endpoint is right and PLAXIS is slow. Moving the
        pin there would abandon a working port over a timing artefact."""

        busy = WorkerOperationError("CONNECTION_FAILED", "did not answer", retryable=True)
        session = self._Session(10007, busy)

        with patch("plaxis_mcp.server.discover_role_ports", return_value=[10007]) as probe:
            with self.assertRaises(WorkerOperationError):
                _connect_session(session, self._config("C:/plaxis"))

        self.assertEqual(session.attempts, [10000])
        probe.assert_not_called()

    def test_the_scripting_port_is_picked_out_of_the_processs_other_ports(self):
        """Measured on PLAXIS 2D V22: one Input process listened on 10003
        ("PLAXIS 2D Input/22.2.0.1078") and 10141 ("sgcWebSockets 4.0.2").
        Owning the process is not what makes a port the scripting server."""

        session = self._Session(
            10003,
            self._dead_port(),
            names={10003: "PLAXIS 2D Input/22.2.0.1078", 10141: "sgcWebSockets 4.0.2"},
        )

        with patch("plaxis_mcp.server.discover_role_ports", return_value=[10003, 10141]):
            result = _connect_session(session, self._config("C:/plaxis"))

        self.assertEqual(result["port_correction"]["port"], 10003)
        self.assertEqual(session.attempts, [10000, 10003])

    def test_the_real_password_only_reaches_a_port_that_named_itself(self):
        """Identity comes first; the connect that carries the password happens
        only against a port that already answered as this role's PLAXIS."""

        session = self._Session(
            10003,
            self._dead_port(),
            names={10003: "PLAXIS 2D Input/22.2.0.1078", 10141: "sgcWebSockets 4.0.2"},
        )

        with patch("plaxis_mcp.server.discover_role_ports", return_value=[10141, 10003]):
            _connect_session(session, self._config("C:/plaxis"))

        # The pinned port is probed first, then each candidate; the password
        # goes only to the one that named itself.
        self.assertEqual(session.identified, [10000, 10141, 10003])
        self.assertNotIn(10141, session.attempts)

    def test_a_port_that_names_the_other_role_is_not_taken(self):
        session = self._Session(
            10003,
            self._dead_port(),
            names={10003: "PLAXIS 2D Output/22.2.0.1078"},
        )

        with patch("plaxis_mcp.server.discover_role_ports", return_value=[10003]):
            with self.assertRaises(WorkerOperationError):
                _connect_session(session, self._config("C:/plaxis"))

        self.assertEqual(session.attempts, [10000])

    def test_two_running_endpoints_are_left_to_the_operator(self):
        """Choosing between them would be choosing which one gets the password."""

        session = self._Session(
            10007,
            self._dead_port(),
            names={
                10007: "PLAXIS 2D Input/22.2.0.1078",
                10008: "PLAXIS 2D Input/22.2.0.1078",
            },
        )

        with patch("plaxis_mcp.server.discover_role_ports", return_value=[10007, 10008]):
            with self.assertRaises(WorkerOperationError):
                _connect_session(session, self._config("C:/plaxis"))

        self.assertEqual(session.attempts, [10000])

    def test_no_running_plaxis_leaves_the_original_failure_intact(self):
        session = self._Session(10007, self._dead_port())

        with patch("plaxis_mcp.server.discover_role_ports", return_value=[]):
            with self.assertRaises(WorkerOperationError) as raised:
                _connect_session(session, self._config("C:/plaxis"))

        self.assertEqual(raised.exception.code, "CONNECTION_FAILED")

    def test_the_correction_is_written_back_to_the_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "input.toml"
            profile.write_text(
                'schema_version = 1\nrole = "input"\nport = 10000\nhost = "127.0.0.1"\n',
                encoding="utf-8",
            )
            session = self._Session(10007, self._dead_port())

            with patch("plaxis_mcp.server.discover_role_ports", return_value=[10007]):
                with patch("plaxis_mcp.server.set_profile_port") as write:
                    result = _connect_session(
                        session, self._config("C:/plaxis", profile)
                    )

            write.assert_called_once_with(str(profile), 10007)
            self.assertTrue(result["port_correction"]["profile_updated"])

    def test_an_unwritable_profile_still_connects_and_says_it_is_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "input.toml"
            session = self._Session(10007, self._dead_port())

            with patch("plaxis_mcp.server.discover_role_ports", return_value=[10007]):
                with patch(
                    "plaxis_mcp.server.set_profile_port", side_effect=OSError("read-only")
                ):
                    result = _connect_session(
                        session, self._config("C:/plaxis", profile)
                    )

            correction = result["port_correction"]
            self.assertFalse(correction["profile_updated"])
            self.assertIn("this session only", correction["detail"])
            self.assertIn("--input-port 10007", correction["detail"])


class RejectedPasswordTests(unittest.TestCase):
    """A rejected password named the problem and no remedy, so the agent
    invented one and asked the user to send the PLAXIS password in the chat.
    That discloses it to the transcript and the model provider, and it cannot
    even work: connect takes no password argument, and the password is read
    from Windows Credential Manager. Both roles are pinned the same way, so
    both must carry the same remedy."""

    class _RejectsEveryRead:
        """The global object of a session the server refuses to decrypt for."""

        def __getattr__(self, name):
            raise RuntimeError("the server failed to decrypt the request")

    def _connect(self, role):
        def factory(**kwargs):
            server = FakeServer()
            server.server_full_name = "PLAXIS 2D %s V22" % role.title()
            return server, self._RejectsEveryRead()

        runtime = WorkerRuntime(role=role, new_server_factory=factory)
        return runtime.handle_request(
            {
                "protocol": 1,
                "id": 1,
                "operation": "connect",
                "params": {
                    "host": "127.0.0.1",
                    "port": 10000,
                    "password": "pw",
                    "timeout": 5,
                },
            }
        )

    def test_each_role_is_told_the_command_that_replaces_its_own_password(self):
        for role in ("input", "output"):
            with self.subTest(role=role):
                result = self._connect(role)

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "AUTHENTICATION_FAILED")
                message = result["error"]["message"]
                self.assertIn(
                    "plaxis-mcp credentials set --profile %s.toml" % role, message
                )
                self.assertIn("hidden input", message)

    def test_neither_role_leaves_room_to_offer_the_chat_as_the_channel(self):
        for role in ("input", "output"):
            with self.subTest(role=role):
                message = self._connect(role)["error"]["message"]

                self.assertIn("Do not ask anyone to type the password", message)
                self.assertIn("takes no password argument", message)

    def test_the_tool_payload_carries_the_remedy_for_both_roles(self):
        for role in (PlaxisRole.INPUT, PlaxisRole.OUTPUT):
            with self.subTest(role=role):
                error = WorkerOperationError(
                    "AUTHENTICATION_FAILED", "rejected", state="disconnected"
                )

                result = _bridge_error_result(error, None, ServerConfig(role=role))

                remediation = result.structuredContent["remediation"]
                self.assertIn("plaxis-mcp credentials set --profile", remediation)
                self.assertIn("%s.toml" % role.value, remediation)
                self.assertIn("Never request the password in chat", remediation)

    def test_an_unrelated_failure_carries_no_credential_advice(self):
        error = WorkerOperationError("TIMEOUT", "too slow", state="uncertain")

        result = _bridge_error_result(error, None, ServerConfig(role=PlaxisRole.INPUT))

        self.assertNotIn("remediation", result.structuredContent)


class ConnectionHonestyTests(unittest.TestCase):
    """WP-I. A connect issued while PLAXIS was still busy failed once, and
    reported retryable: false. The agent read that as permanent and answered
    from its own knowledge -- producing a plausible ground-profile table for a
    model it had never opened."""

    def _connect(self, runtime):
        return runtime.handle_request(
            {
                "protocol": 1,
                "id": 1,
                "operation": "connect",
                "params": {"host": "127.0.0.1", "port": 10000, "password": "pw", "timeout": 15},
            }
        )

    def test_a_busy_server_is_retried_and_then_succeeds(self):
        attempts = []

        def factory(**kwargs):
            attempts.append(kwargs)
            if len(attempts) < 2:
                raise OSError("The read operation timed out")
            return FakeServer(), FakeObject("Root")

        runtime = WorkerRuntime(role="input", new_server_factory=factory)
        with patch("plaxis_mcp.worker_runtime.time.sleep") as sleep:
            result = self._connect(runtime)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["result"]["connected"])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleep.call_count, 1)

    def test_a_server_that_never_answers_fails_retryably_after_every_attempt(self):
        attempts = []

        def factory(**kwargs):
            attempts.append(kwargs)
            raise OSError("Read timed out")

        runtime = WorkerRuntime(role="input", new_server_factory=factory)
        with patch("plaxis_mcp.worker_runtime.time.sleep"):
            result = self._connect(runtime)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "CONNECTION_FAILED")
        self.assertTrue(result["error"]["retryable"])
        self.assertEqual(len(attempts), CONNECT_ATTEMPTS)
        self.assertIn("No model data was read", result["error"]["message"])

    def test_a_dead_port_fails_on_the_first_attempt_and_not_retryably(self):
        attempts = []

        def factory(**kwargs):
            attempts.append(kwargs)
            raise OSError(
                "No connection could be made because the target machine actively refused it"
            )

        runtime = WorkerRuntime(role="input", new_server_factory=factory)
        with patch("plaxis_mcp.worker_runtime.time.sleep") as sleep:
            result = self._connect(runtime)

        self.assertEqual(len(attempts), 1)
        self.assertEqual(sleep.call_count, 0)
        self.assertFalse(result["error"]["retryable"])
        self.assertIn("Nothing is listening", result["error"]["message"])
        self.assertIn("No model data was read", result["error"]["message"])

    def test_an_unrecognised_failure_is_not_retried_or_called_a_timeout(self):
        """Retrying everything that was not a refused port reported a
        permanent ValueError three attempts later as "did not answer within
        the connect timeout", retryable: true -- a made-up diagnosis and a
        made-up promise that retrying would help."""

        attempts = []

        def factory(**kwargs):
            attempts.append(kwargs)
            raise ValueError("invalid endpoint response")

        runtime = WorkerRuntime(role="input", new_server_factory=factory)
        with patch("plaxis_mcp.worker_runtime.time.sleep") as sleep:
            result = self._connect(runtime)

        self.assertEqual(len(attempts), 1)
        self.assertEqual(sleep.call_count, 0)
        self.assertFalse(result["error"]["retryable"])
        self.assertNotIn("timeout", result["error"]["message"])
        self.assertIn("does not recognise", result["error"]["message"])
        self.assertIn("No model data was read", result["error"]["message"])
        self.assertEqual(result["error"]["plaxis_message"], "invalid endpoint response")

    def test_a_failed_connect_stays_distinguishable_from_a_read_model(self):
        def factory(**kwargs):
            raise OSError("Read timed out")

        runtime = WorkerRuntime(role="input", new_server_factory=factory)
        with patch("plaxis_mcp.worker_runtime.time.sleep"):
            result = self._connect(runtime)

        self.assertEqual(result["error"]["state"], "disconnected")

    def test_the_host_deadline_covers_the_whole_retry_sequence(self):
        # A host deadline shorter than the worker's retries would fault the
        # worker mid-sequence and turn a recoverable connect into an uncertain
        # session -- the opposite of what this work package is for.
        self.assertGreater(connect_deadline(15.0, 10.0), 15.0 * CONNECT_ATTEMPTS)


class ViewResultsTests(unittest.TestCase):
    """Output reads only what its application has open, and PLAXIS's own
    "view" takes a phase object -- which call_method cannot pass. Live, every
    Output tool answered "No active project" against a model that had just
    been calculated, so this is the only route to results."""

    class Root:
        def __init__(self):
            self.Phase_3 = FakeObject("Phase_3", type_name="Phase")
            self.calls = []

        def view(self, phase):
            self.calls.append(phase)
            return True

    def test_view_results_passes_the_resolved_phase_not_its_name(self):
        root = self.Root()
        result = call(input_runtime(root), "view_results", phase="Phase_3")

        self.assertTrue(result["ok"], result)
        self.assertIs(root.calls[0], root.Phase_3)
        self.assertNotIsInstance(root.calls[0], str)
        self.assertEqual(result["result"]["phase"]["path"], "Phase_3")

    def test_view_results_rejects_a_phase_that_does_not_exist(self):
        result = call(input_runtime(self.Root()), "view_results", phase="Phase_99")

        self.assertEqual(result["error"]["code"], "INVALID_PATH")

    def test_view_results_is_not_offered_on_the_output_role(self):
        server = create_server(
            ServerConfig(role=PlaxisRole.OUTPUT),
            session_factory=lambda: type("S", (), {
                "status": lambda self: {"state": "connected", "connected": True},
                "invoke": lambda self, operation, **params: {},
            })(),
        )
        names = {tool.name for tool in asyncio.run(server.list_tools())}

        self.assertNotIn("view_results", names)
        self.assertIn("get_results", names)


class ExistingListingTests(unittest.TestCase):
    """WP-B applies to "every object in any tool result" -- including the two
    listings that predate it and are the ones an agent reads first."""

    def _root(self):
        root = type("Root", (object,), {})()
        root.Phases = [FakePhase("InitialPhase"), FakePhase("Phase_1")]
        root.Materials = [FakeObject("SoilMat_1", identification="SZ", type_name="SoilMat")]
        for item in root.Phases + root.Materials:
            setattr(root, item.Name.value, item)
        return root

    def test_list_phases_carries_a_path_that_resolves(self):
        root = self._root()
        result = call(input_runtime(root), "list_phases")

        paths = [phase["path"] for phase in result["result"]["phases"]]
        self.assertEqual(paths, ["InitialPhase", "Phase_1"])
        self.assertIs(resolve_path(root, paths[1]), root.Phases[1])

    def test_list_materials_carries_a_path_that_resolves(self):
        root = self._root()
        result = call(input_runtime(root), "list_materials")

        material = result["result"]["materials"][0]
        self.assertEqual(material["path"], "SoilMat_1")
        self.assertEqual(material["identification"], "SZ")
        self.assertIs(resolve_path(root, material["path"]), root.Materials[0])


class WorkerErrorFieldTests(unittest.TestCase):
    def test_an_error_without_a_vendor_message_keeps_its_existing_shape(self):
        error = WorkerError("INVALID_ARGUMENT", "no")

        self.assertIsNone(error.plaxis_message)
        self.assertFalse(error.plaxis_message_truncated)
        self.assertIsNone(error.details)

    def test_the_envelope_omits_vendor_fields_when_there_is_nothing_to_say(self):
        result = call(input_runtime(FakeObject("Root")), "list_objects", kind="unknown-kind")

        self.assertNotIn("plaxis_message", result["error"])
        self.assertNotIn("details", result["error"])


if __name__ == "__main__":
    unittest.main()
