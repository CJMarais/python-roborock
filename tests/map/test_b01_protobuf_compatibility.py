"""Tests for the generated B01 protobuf binding."""

from roborock.map.proto.b01_scmap_pb2 import RobotMap


def test_b01_protobuf_binding_imports_with_supported_runtime() -> None:
    """The checked-in binding must import with the supported protobuf runtime."""
    assert RobotMap.DESCRIPTOR.full_name == "b01.scmap.RobotMap"
