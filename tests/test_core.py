import unittest

from plaxis_mcp.core import PlaxisSession, parse_path, resolve_path, serialize_value


class FakeProperty:
    def __init__(self, value):
        self.value = value


class FakeProxy:
    def __init__(self):
        self._guid = "guid-1"
        self.Name = FakeProperty("Soil_1")
        self.TypeName = FakeProperty("Soil")
        self.children = [FakeProperty("a"), FakeProperty("b")]

    def __repr__(self):
        return "<FakeProxy guid-1>"

    def save(self):
        return True

    def saveas(self, filename):
        return filename


class FakeRoot:
    def __init__(self):
        self.Phases = [FakeProperty("Phase_1"), FakeProperty("Phase_2")]
        self.Soil_1 = FakeProxy()
        self.Materials = [FakeProxy()]
        self.ProjectTitle = FakeProperty("Demo")
        self.ProjectDescription = FakeProperty("Example project")
        self.Filename = FakeProperty("demo.p2dx")
        self.UnitForce = FakeProperty("kN")
        self.UnitLength = FakeProperty("m")
        self.UnitTime = FakeProperty("day")

    def save(self):
        return True

    def saveas(self, filename):
        return filename


class FakeServer:
    def new(self):
        return True

    def open(self, filename):
        return filename

    def close(self):
        return True

    def recover(self):
        return True


class CoreTests(unittest.TestCase):
    def test_parse_path_with_index(self):
        self.assertEqual(parse_path("Phases[1]"), ["Phases", 1])

    def test_parse_path_with_nested_members(self):
        self.assertEqual(parse_path("Soil_1.Name"), ["Soil_1", "Name"])

    def test_resolve_path(self):
        root = FakeRoot()
        value = resolve_path(root, "Soil_1.Name")
        self.assertEqual(value.value, "Soil_1")

    def test_serialize_property(self):
        payload = serialize_value(FakeProperty("x"))
        self.assertEqual(payload["kind"], "property")
        self.assertEqual(payload["value"], "x")

    def test_serialize_proxy(self):
        payload = serialize_value(FakeProxy())
        self.assertEqual(payload["kind"], "object")
        self.assertEqual(payload["guid"], "guid-1")
        self.assertEqual(payload["name"], "Soil_1")

    def test_project_helpers(self):
        session = PlaxisSession()
        session._server = FakeServer()
        session._global = FakeRoot()

        self.assertEqual(session.new_project()["action"], "new_project")
        self.assertEqual(session.open_project("a.p2dx")["filename"], "a.p2dx")
        self.assertEqual(session.close_project()["action"], "close_project")
        self.assertEqual(session.recover_project()["action"], "recover_project")
        self.assertEqual(session.save_project()["action"], "save_project")
        self.assertEqual(session.save_project("b.p2dx")["filename"], "b.p2dx")

    def test_list_helpers(self):
        session = PlaxisSession()
        session._server = FakeServer()
        session._global = FakeRoot()

        self.assertEqual(session.list_phases()["count"], 2)
        self.assertEqual(session.list_materials()["count"], 1)
        self.assertEqual(session.project_info()["project_title"], "Demo")


if __name__ == "__main__":
    unittest.main()
