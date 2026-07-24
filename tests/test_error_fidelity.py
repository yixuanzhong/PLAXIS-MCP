"""Error fidelity and parameter-shape correctness (WP-A .. WP-I).

Every case here comes from a measured failure in the 2026-07-23 Caros CLI
smoke test against PLAXIS 2D V22, cross-read against the PLAXIS scripting
console transcripts captured during the same runs: the console knew exactly
what was wrong, and none of it reached the caller.
"""

import asyncio
import unittest
from unittest.mock import patch

from plaxis_mcp.bridge import WorkerOperationError
from plaxis_mcp.server import PlaxisRole, ServerConfig, create_server
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

    def test_a_writable_neighbour_property_is_untouched_by_the_rule(self):
        runtime, zone = self._runtime()
        result = call(runtime, "set_property", path="Soillayer_2.Zones[0].Bottom", value=-6.0)

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
