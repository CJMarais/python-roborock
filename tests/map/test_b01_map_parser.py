import base64
import gzip
import hashlib
import io
import json
import zlib
from pathlib import Path

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from PIL import Image

from roborock.exceptions import RoborockException
from roborock.map.b01_map_parser import (
    B01MapOverlays,
    B01MapParser,
    B01MapParserConfig,
    B01RoomBoundary,
    B01RoomLabel,
    _extract_calibration_points,
    _extract_map_overlays,
    _extract_room_boundaries,
    _extract_room_labels,
    _extract_rooms,
    _parse_scmap_payload,
    _split_path,
)
from roborock.map.proto.b01_scmap_pb2 import RobotMap  # type: ignore[attr-defined]
from roborock.protocols.b01_q7_protocol import create_map_key, decode_map_payload

FIXTURE = Path(__file__).resolve().parent / "testdata" / "raw-mqtt-map301.bin.inflated.bin.gz"


def _derive_map_key(serial: str, model: str) -> bytes:
    model_suffix = model.split(".")[-1]
    model_key = (model_suffix + "0" * 16)[:16].encode()
    material = f"{serial}+{model_suffix}+{serial}".encode()
    encrypted = AES.new(model_key, AES.MODE_ECB).encrypt(pad(material, AES.block_size))
    md5 = hashlib.md5(base64.b64encode(encrypted), usedforsecurity=False).hexdigest()
    return md5[8:24].encode()


def test_b01_map_parser_decodes_and_renders_fixture() -> None:
    serial = "testsn012345"
    model = "roborock.vacuum.sc05"
    inflated = gzip.decompress(FIXTURE.read_bytes())

    compressed = zlib.compress(inflated)
    map_key = create_map_key(serial, model)
    encrypted = AES.new(map_key.key, AES.MODE_ECB).encrypt(pad(compressed.hex().encode(), AES.block_size))
    payload = base64.b64encode(encrypted)

    parser = B01MapParser()
    inflated_payload = decode_map_payload(payload, map_key=map_key)
    parsed = parser.parse(inflated_payload)

    assert parsed.image_content is not None
    assert parsed.image_content.startswith(b"\x89PNG\r\n\x1a\n")
    assert parsed.map_data is not None

    # The fixture includes 10 rooms with names room1..room10.
    assert parsed.map_data.additional_parameters["room_names"] == {
        10: "room1",
        11: "room2",
        12: "room3",
        13: "room4",
        14: "room5",
        15: "room6",
        16: "room7",
        17: "room8",
        18: "room9",
        19: "room10",
    }
    assert len(parsed.map_data.additional_parameters["calibration_points"]) == 3
    assert len(parsed.map_data.additional_parameters["rooms"]) == 10
    assert parsed.map_data.rooms is not None
    assert len(parsed.map_data.rooms) == 10

    # Image should be scaled by default.
    img = Image.open(io.BytesIO(parsed.image_content))
    assert img.size == (340 * 4, 300 * 4)


def test_b01_scmap_parser_maps_observed_schema_fields() -> None:
    payload = RobotMap()
    payload.mapType = 1
    payload.mapExtInfo.taskBeginDate = 100
    payload.mapExtInfo.mapUploadDate = 200
    payload.mapExtInfo.mapValid = 1
    payload.mapExtInfo.mapVersion = 3
    payload.mapExtInfo.boudaryInfo.mapMd5 = "md5"
    payload.mapExtInfo.boudaryInfo.vMinX = 10
    payload.mapExtInfo.boudaryInfo.vMaxX = 20
    payload.mapExtInfo.boudaryInfo.vMinY = 30
    payload.mapExtInfo.boudaryInfo.vMaxY = 40
    payload.mapHead.mapHeadId = 7
    payload.mapHead.sizeX = 2
    payload.mapHead.sizeY = 2
    payload.mapHead.minX = 1.5
    payload.mapHead.minY = 2.5
    payload.mapHead.maxX = 3.5
    payload.mapHead.maxY = 4.5
    payload.mapHead.resolution = 0.05
    payload.mapData.mapData = bytes([0, 127, 128, 128])

    room_one = payload.roomDataInfo.add()
    room_one.roomId = 42
    room_one.roomName = "Kitchen"
    room_one.cleanState = 1
    room_one.roomNamePost.x = 11.25
    room_one.roomNamePost.y = 22.5
    room_one.colorId = 7
    room_one.global_seq = 9

    room_two = payload.roomDataInfo.add()
    room_two.roomId = 99
    room_two.cleanState = 0

    parsed = _parse_scmap_payload(payload.SerializeToString())

    assert parsed.mapType == 1
    assert parsed.HasField("mapExtInfo")
    assert parsed.mapExtInfo.taskBeginDate == 100
    assert parsed.mapExtInfo.mapUploadDate == 200
    assert parsed.mapExtInfo.HasField("boudaryInfo")
    assert parsed.mapExtInfo.boudaryInfo.vMaxY == 40
    assert parsed.HasField("mapHead")
    assert parsed.mapHead.mapHeadId == 7
    assert parsed.mapHead.sizeX == 2
    assert parsed.mapHead.sizeY == 2
    assert parsed.mapHead.resolution == pytest.approx(0.05)
    assert parsed.HasField("mapData")
    assert parsed.mapData.HasField("mapData")
    assert parsed.mapData.mapData == bytes([0, 127, 128, 128])
    assert parsed.roomDataInfo[0].roomId == 42
    assert parsed.roomDataInfo[0].roomName == "Kitchen"
    assert parsed.roomDataInfo[0].HasField("roomNamePost")
    assert parsed.roomDataInfo[0].roomNamePost.x == pytest.approx(11.25)
    assert parsed.roomDataInfo[0].roomNamePost.y == pytest.approx(22.5)
    assert parsed.roomDataInfo[0].colorId == 7
    assert parsed.roomDataInfo[0].global_seq == 9
    assert parsed.roomDataInfo[1].roomId == 99
    assert not parsed.roomDataInfo[1].HasField("roomName")


def test_b01_map_parser_projects_room_labels() -> None:
    payload = RobotMap()
    payload.mapHead.sizeX = 4
    payload.mapHead.sizeY = 3
    payload.mapHead.minX = 10
    payload.mapHead.minY = 20
    payload.mapHead.resolution = 0.5
    room = payload.roomDataInfo.add()
    room.roomId = 42
    room.roomName = "Kitchen"
    room.roomNamePost.x = 11
    room.roomNamePost.y = 20.5
    room.colorId = 7

    assert _extract_room_labels(payload) == [B01RoomLabel(name="Kitchen", x=2, y=1, color_id=7)]


def test_b01_map_parser_builds_calibration_points() -> None:
    payload = RobotMap()
    payload.mapHead.sizeX = 4
    payload.mapHead.sizeY = 3
    payload.mapHead.minX = 10
    payload.mapHead.minY = 20
    payload.mapHead.resolution = 0.5

    assert _extract_calibration_points(payload, scale=4) == [
        {"vacuum": {"x": 10, "y": 20}, "map": {"x": 0, "y": 8}},
        {"vacuum": {"x": 11.5, "y": 20}, "map": {"x": 12, "y": 8}},
        {"vacuum": {"x": 10, "y": 21}, "map": {"x": 0, "y": 0}},
    ]


def test_b01_map_parser_extracts_card_rooms_in_vacuum_coordinates() -> None:
    payload = RobotMap()
    payload.mapHead.sizeX = 4
    payload.mapHead.sizeY = 3
    payload.mapHead.minX = 10
    payload.mapHead.minY = 20
    payload.mapHead.resolution = 0.5
    room = payload.roomDataInfo.add()
    room.roomId = 42
    room.roomName = "Kitchen"
    room.roomNamePost.x = 10.5
    room.roomNamePost.y = 20.5
    boundary = payload.roomBoundaryInfo.add()
    boundary.roomId = 42
    for x, y in ((0, 0), (2, 0), (2, 2), (0, 2)):
        point = boundary.points.add()
        point.x = x
        point.y = y

    assert _extract_rooms(payload) == {
        42: {
            "name": "Kitchen",
            "x0": 10,
            "y0": 20,
            "x1": 11,
            "y1": 21,
            "x": 10.5,
            "y": 20.5,
            "outline": [[10, 20], [11, 20], [11, 21], [10, 21]],
        }
    }


def test_b01_map_parser_bounds_card_room_metadata_size() -> None:
    payload = RobotMap()
    payload.mapHead.sizeX = 1000
    payload.mapHead.sizeY = 1000
    payload.mapHead.minX = 10000
    payload.mapHead.minY = 20000
    payload.mapHead.resolution = 50

    for room_id in range(1, 13):
        room = payload.roomDataInfo.add()
        room.roomId = room_id
        room.roomName = f"Room {room_id}"
        boundary = payload.roomBoundaryInfo.add()
        boundary.roomId = room_id
        for index in range(200):
            point = boundary.points.add()
            point.x = room_id * 50 + index % 50
            point.y = room_id * 50 + index // 50

    rooms = _extract_rooms(payload)

    assert len(rooms) == 12
    assert all(len(room["outline"]) == 20 for room in rooms.values())
    assert len(json.dumps(rooms, separators=(",", ":"))) < 16384


def test_b01_map_parser_calibration_matches_card_metadata() -> None:
    payload = RobotMap()
    payload.mapHead.sizeX = 4
    payload.mapHead.sizeY = 3
    payload.mapHead.minX = 10
    payload.mapHead.minY = 20
    payload.mapHead.resolution = 0.5
    payload.mapData.mapData = bytes([128]) * 12

    parsed = B01MapParser(B01MapParserConfig(map_scale=4)).parse(payload.SerializeToString())

    assert parsed.map_data is not None
    assert parsed.map_data.calibration() == [
        {"vacuum": {"x": 0, "y": 0}, "map": {"x": -80, "y": 168}},
        {"vacuum": {"x": 5, "y": 0}, "map": {"x": -40, "y": 168}},
        {"vacuum": {"x": 0, "y": 5}, "map": {"x": -80, "y": 128}},
    ]


def test_b01_map_parser_renders_room_label() -> None:
    payload = RobotMap()
    payload.mapHead.sizeX = 40
    payload.mapHead.sizeY = 30
    payload.mapHead.minX = 0
    payload.mapHead.minY = 0
    payload.mapHead.resolution = 1
    payload.mapData.mapData = bytes([128]) * (40 * 30)
    room = payload.roomDataInfo.add()
    room.roomId = 7
    room.roomName = "Kitchen"
    room.roomNamePost.x = 20
    room.roomNamePost.y = 15
    room.colorId = 7

    with_labels = B01MapParser(B01MapParserConfig(map_scale=4)).parse(payload.SerializeToString())
    without_labels = B01MapParser(B01MapParserConfig(map_scale=4, show_room_labels=False)).parse(
        payload.SerializeToString()
    )

    assert with_labels.image_content != without_labels.image_content


def test_b01_map_parser_extracts_room_boundary() -> None:
    payload = RobotMap()
    payload.mapHead.sizeX = 4
    payload.mapHead.sizeY = 3
    room = payload.roomDataInfo.add()
    room.roomId = 42
    room.colorId = 7
    boundary = payload.roomBoundaryInfo.add()
    boundary.roomId = 42
    for x, y in ((0, 0), (1, 0), (1, 1)):
        point = boundary.points.add()
        point.x = x
        point.y = y

    assert _extract_room_boundaries(payload) == [
        B01RoomBoundary(
            room_id=42,
            color_id=7,
            points=((0, 2), (1, 2), (1, 1)),
        )
    ]


def test_b01_map_parser_renders_room_boundary() -> None:
    payload = RobotMap()
    payload.mapHead.sizeX = 4
    payload.mapHead.sizeY = 3
    payload.mapData.mapData = bytes([128]) * 12
    boundary = payload.roomBoundaryInfo.add()
    boundary.roomId = 7
    for x, y in ((0, 0), (1, 0), (1, 1)):
        point = boundary.points.add()
        point.x = x
        point.y = y

    with_boundaries = B01MapParser(B01MapParserConfig(map_scale=4)).parse(payload.SerializeToString())
    without_boundaries = B01MapParser(B01MapParserConfig(map_scale=4, show_room_boundaries=False)).parse(
        payload.SerializeToString()
    )

    assert with_boundaries.image_content != without_boundaries.image_content


def test_b01_map_parser_extracts_dynamic_overlays() -> None:
    payload = RobotMap()
    payload.mapHead.sizeX = 4
    payload.mapHead.sizeY = 3
    payload.mapHead.minX = 10
    payload.mapHead.minY = 20
    payload.mapHead.resolution = 0.5
    path_point = payload.cleanPathInfo.points.add()
    path_point.x = 11
    path_point.y = 20.5
    payload.chargerInfo.x = 10.5
    payload.chargerInfo.y = 20.5
    payload.robotPositionInfo.x = 11.5
    payload.robotPositionInfo.y = 21

    assert _extract_map_overlays(payload) == B01MapOverlays(
        path=((2, 1),),
        charger=(1, 1),
        robot=(3, 0),
    )


def test_b01_map_parser_renders_dynamic_overlays() -> None:
    payload = RobotMap()
    payload.mapHead.sizeX = 40
    payload.mapHead.sizeY = 30
    payload.mapHead.minX = 0
    payload.mapHead.minY = 0
    payload.mapHead.resolution = 1
    payload.mapData.mapData = bytes([128]) * (40 * 30)
    for x, y in ((2, 2), (20, 15), (35, 25)):
        point = payload.cleanPathInfo.points.add()
        point.x = x
        point.y = y
    payload.chargerInfo.x = 2
    payload.chargerInfo.y = 2
    payload.robotPositionInfo.x = 35
    payload.robotPositionInfo.y = 25

    with_overlays = B01MapParser(B01MapParserConfig(map_scale=4)).parse(payload.SerializeToString())
    without_overlays = B01MapParser(
        B01MapParserConfig(
            map_scale=4,
            show_path=False,
            show_charger=False,
            show_robot=False,
        )
    ).parse(payload.SerializeToString())

    assert with_overlays.image_content != without_overlays.image_content
    assert with_overlays.map_data is not None
    assert with_overlays.map_data.path is not None
    assert with_overlays.map_data.charger is not None
    assert with_overlays.map_data.vacuum_position is not None


def test_b01_map_parser_splits_discontinuous_path() -> None:
    assert _split_path(((0, 0), (1, 1), (20, 20), (21, 21))) == [
        [(0, 0), (1, 1)],
        [(20, 20), (21, 21)],
    ]


def test_b01_map_parser_rejects_invalid_payload() -> None:
    parser = B01MapParser()
    with pytest.raises(RoborockException, match="Failed to parse B01 SCMap"):
        parser.parse(b"not a map")
