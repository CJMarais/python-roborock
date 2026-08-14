"""Module for parsing B01/Q7 map content.

The inner SCMap blob is parsed with protobuf messages generated from
`roborock/map/proto/b01_scmap.proto`.
"""

import io
from dataclasses import dataclass

from google.protobuf.message import DecodeError
from PIL import Image, ImageDraw
from vacuum_map_parser_base.config.color import ColorsPalette
from vacuum_map_parser_base.config.image_config import ImageConfig
from vacuum_map_parser_base.map_data import ImageData, MapData, Path, Point

from roborock.exceptions import RoborockException
from roborock.map.proto.b01_scmap_pb2 import RobotMap  # type: ignore[attr-defined]

from .map_parser import ParsedMapData

_MAP_FILE_FORMAT = "PNG"


@dataclass
class B01MapParserConfig:
    """Configuration for the B01/Q7 map parser."""

    map_scale: int = 4
    """Scale factor for the rendered map image."""

    show_room_labels: bool = True
    """Draw room names at the positions supplied by the device."""

    show_room_boundaries: bool = True
    """Draw room outlines from the boundary points supplied by the device."""

    show_path: bool = True
    """Draw the cleaning path supplied by the device."""

    show_charger: bool = True
    """Draw the charger position supplied by the device."""

    show_robot: bool = True
    """Draw the current robot position supplied by the device."""


@dataclass(frozen=True)
class B01RoomLabel:
    """Room label projected into the map grid."""

    name: str
    x: float
    y: float
    color_id: int


@dataclass(frozen=True)
class B01RoomBoundary:
    """Room boundary represented in the rendered map grid."""

    room_id: int
    color_id: int
    points: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class B01MapOverlays:
    """Dynamic overlays projected into the rendered map grid."""

    path: tuple[tuple[float, float], ...]
    charger: tuple[float, float] | None
    robot: tuple[float, float] | None


class B01MapParser:
    """Decoder/parser for B01/Q7 SCMap payloads."""

    def __init__(self, config: B01MapParserConfig | None = None) -> None:
        self._config = config or B01MapParserConfig()

    def parse(self, payload: bytes) -> ParsedMapData:
        """Parse an inflated SCMap payload and return a PNG + MapData."""
        parsed = _parse_scmap_payload(payload)
        size_x, size_y, grid = _extract_grid(parsed)
        room_names = _extract_room_names(parsed)
        room_labels = _extract_room_labels(parsed)
        room_boundaries = _extract_room_boundaries(parsed)
        overlays = _extract_map_overlays(parsed)

        image = _render_occupancy_image(
            grid,
            size_x=size_x,
            size_y=size_y,
            scale=self._config.map_scale,
            room_labels=room_labels if self._config.show_room_labels else None,
            room_boundaries=(room_boundaries if self._config.show_room_boundaries else None),
            path=overlays.path if self._config.show_path else None,
            charger=overlays.charger if self._config.show_charger else None,
            robot=overlays.robot if self._config.show_robot else None,
        )

        map_data = MapData()
        map_data.image = ImageData(
            size=size_x * size_y,
            top=0,
            left=0,
            height=size_y,
            width=size_x,
            image_config=ImageConfig(scale=self._config.map_scale),
            data=image,
            img_transformation=lambda p: p,
        )
        if room_names:
            map_data.additional_parameters["room_names"] = room_names
        if overlays.path:
            path_runs = [[Point(x, y) for x, y in run] for run in _split_path(overlays.path)]
            map_data.path = Path(sum(map(len, path_runs)), 1, 0, path_runs)
        if overlays.charger:
            map_data.charger = Point(*overlays.charger)
        if overlays.robot:
            map_data.vacuum_position = Point(*overlays.robot)

        image_bytes = io.BytesIO()
        image.save(image_bytes, format=_MAP_FILE_FORMAT)

        return ParsedMapData(
            image_content=image_bytes.getvalue(),
            map_data=map_data,
        )


def _parse_scmap_payload(payload: bytes) -> RobotMap:
    """Parse inflated SCMap bytes into a generated protobuf message."""
    parsed = RobotMap()
    try:
        parsed.ParseFromString(payload)
    except DecodeError as err:
        raise RoborockException("Failed to parse B01 SCMap") from err
    return parsed


def _extract_grid(parsed: RobotMap) -> tuple[int, int, bytes]:
    if not parsed.HasField("mapHead") or not parsed.HasField("mapData"):
        raise RoborockException("Failed to parse B01 map header/grid")

    size_x = parsed.mapHead.sizeX if parsed.mapHead.HasField("sizeX") else 0
    size_y = parsed.mapHead.sizeY if parsed.mapHead.HasField("sizeY") else 0
    if not size_x or not size_y or not parsed.mapData.HasField("mapData"):
        raise RoborockException("Failed to parse B01 map header/grid")

    map_data = parsed.mapData.mapData
    expected_len = size_x * size_y
    if len(map_data) < expected_len:
        raise RoborockException("B01 map data shorter than expected dimensions")

    return size_x, size_y, map_data[:expected_len]


def _extract_room_names(parsed: RobotMap) -> dict[int, str]:
    """Extract the room ID to display-name mapping."""
    room_names: dict[int, str] = {}
    for room in parsed.roomDataInfo:
        if room.HasField("roomId"):
            room_id = room.roomId
            room_names[room_id] = room.roomName if room.HasField("roomName") else f"Room {room_id}"
    return room_names


def _extract_room_labels(parsed: RobotMap) -> list[B01RoomLabel]:
    """Project device-supplied room label positions into the map grid."""
    if not parsed.HasField("mapHead"):
        return []
    header = parsed.mapHead
    if (
        not header.HasField("resolution")
        or header.resolution <= 0
        or not header.HasField("minX")
        or not header.HasField("minY")
        or not header.HasField("sizeX")
        or not header.HasField("sizeY")
    ):
        return []

    labels: list[B01RoomLabel] = []
    for room in parsed.roomDataInfo:
        if not room.HasField("roomId") or not room.HasField("roomNamePost"):
            continue
        x = (room.roomNamePost.x - header.minX) / header.resolution
        y = header.sizeY - 1 - (room.roomNamePost.y - header.minY) / header.resolution
        if not 0 <= x < header.sizeX or not 0 <= y < header.sizeY:
            continue
        labels.append(
            B01RoomLabel(
                name=room.roomName if room.HasField("roomName") else f"Room {room.roomId}",
                x=x,
                y=y,
                color_id=room.colorId if room.HasField("colorId") else room.roomId,
            )
        )
    return labels


def _extract_room_boundaries(parsed: RobotMap) -> list[B01RoomBoundary]:
    """Extract device-supplied room boundaries in rendered-grid orientation."""
    if not parsed.HasField("mapHead"):
        return []
    header = parsed.mapHead
    if not header.HasField("sizeX") or not header.HasField("sizeY"):
        return []
    room_colors = {
        room.roomId: room.colorId if room.HasField("colorId") else room.roomId
        for room in parsed.roomDataInfo
        if room.HasField("roomId")
    }

    boundaries: list[B01RoomBoundary] = []
    for boundary in parsed.roomBoundaryInfo:
        if not boundary.HasField("roomId"):
            continue
        points = tuple(
            (point.x, header.sizeY - 1 - point.y)
            for point in boundary.points
            if point.HasField("x")
            and point.HasField("y")
            and 0 <= point.x < header.sizeX
            and 0 <= point.y < header.sizeY
        )
        if len(points) < 2:
            continue
        boundaries.append(
            B01RoomBoundary(
                room_id=boundary.roomId,
                color_id=room_colors.get(boundary.roomId, boundary.roomId),
                points=points,
            )
        )
    return boundaries


def _project_map_point(parsed: RobotMap, x: float, y: float) -> tuple[float, float] | None:
    """Project a device map coordinate into the rendered map grid."""
    if not parsed.HasField("mapHead"):
        return None
    header = parsed.mapHead
    if (
        not header.HasField("resolution")
        or header.resolution <= 0
        or not header.HasField("minX")
        or not header.HasField("minY")
        or not header.HasField("sizeX")
        or not header.HasField("sizeY")
    ):
        return None
    grid_x = (x - header.minX) / header.resolution
    grid_y = header.sizeY - 1 - (y - header.minY) / header.resolution
    if not 0 <= grid_x < header.sizeX or not 0 <= grid_y < header.sizeY:
        return None
    return grid_x, grid_y


def _extract_map_overlays(parsed: RobotMap) -> B01MapOverlays:
    """Extract path, charger, and robot overlays from the Q7 map payload."""
    path = tuple(
        projected
        for point in parsed.cleanPathInfo.points
        if point.HasField("x")
        and point.HasField("y")
        and (projected := _project_map_point(parsed, point.x, point.y)) is not None
    )
    charger = None
    if parsed.HasField("chargerInfo") and parsed.chargerInfo.HasField("x") and parsed.chargerInfo.HasField("y"):
        charger = _project_map_point(parsed, parsed.chargerInfo.x, parsed.chargerInfo.y)
    robot = None
    if (
        parsed.HasField("robotPositionInfo")
        and parsed.robotPositionInfo.HasField("x")
        and parsed.robotPositionInfo.HasField("y")
    ):
        robot = _project_map_point(parsed, parsed.robotPositionInfo.x, parsed.robotPositionInfo.y)
    return B01MapOverlays(path=path, charger=charger, robot=robot)


def _draw_room_labels(image: Image.Image, labels: list[B01RoomLabel], *, scale: int) -> None:
    """Draw readable room-name pills without inventing room geometry."""
    draw = ImageDraw.Draw(image)
    palette = ColorsPalette()
    padding = max(2, scale)
    radius = max(2, scale)
    for label in labels:
        position = (round(label.x * scale), round(label.y * scale))
        bounds = draw.textbbox(position, label.name, anchor="mm")
        background = (
            bounds[0] - padding,
            bounds[1] - padding,
            bounds[2] + padding,
            bounds[3] + padding,
        )
        draw.rounded_rectangle(background, radius=radius, fill=palette.get_room_color(label.color_id))
        draw.text(position, label.name, fill=(0, 0, 0), anchor="mm")


def _draw_room_boundaries(image: Image.Image, boundaries: list[B01RoomBoundary]) -> None:
    """Draw contiguous room-boundary runs without bridging separate contours."""
    draw = ImageDraw.Draw(image)
    palette = ColorsPalette()
    for boundary in boundaries:
        color = palette.get_room_color(boundary.color_id)
        run = [boundary.points[0]]
        for point in boundary.points[1:]:
            previous = run[-1]
            if max(abs(point[0] - previous[0]), abs(point[1] - previous[1])) > 2:
                if len(run) > 1:
                    draw.line(run, fill=color, width=1)
                run = [point]
            else:
                run.append(point)
        if len(run) > 1:
            draw.line(run, fill=color, width=1)


def _draw_map_overlays(
    image: Image.Image,
    *,
    path: tuple[tuple[float, float], ...] | None,
    charger: tuple[float, float] | None,
    robot: tuple[float, float] | None,
    scale: int,
) -> None:
    """Draw the dynamic path, charger, and robot overlays."""
    draw = ImageDraw.Draw(image)
    if path:
        for run in _split_path(path):
            draw.line(
                [(x * scale, y * scale) for x, y in run],
                fill=(30, 144, 255),
                width=max(1, scale // 2),
            )
    if charger:
        x, y = (coordinate * scale for coordinate in charger)
        radius = max(3, scale)
        draw.rectangle((x - radius, y - radius, x + radius, y + radius), fill=(35, 110, 190))
    if robot:
        x, y = (coordinate * scale for coordinate in robot)
        radius = max(4, scale + 1)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(220, 55, 55))


def _split_path(
    path: tuple[tuple[float, float], ...],
) -> list[list[tuple[float, float]]]:
    """Split path runs at discontinuities instead of drawing connector lines."""
    runs = [[path[0]]]
    for point in path[1:]:
        previous = runs[-1][-1]
        if max(abs(point[0] - previous[0]), abs(point[1] - previous[1])) > 5:
            runs.append([point])
        else:
            runs[-1].append(point)
    return [run for run in runs if len(run) > 1]


def _render_occupancy_image(
    grid: bytes,
    *,
    size_x: int,
    size_y: int,
    scale: int,
    room_labels: list[B01RoomLabel] | None = None,
    room_boundaries: list[B01RoomBoundary] | None = None,
    path: tuple[tuple[float, float], ...] | None = None,
    charger: tuple[float, float] | None = None,
    robot: tuple[float, float] | None = None,
) -> Image.Image:
    """Render the B01 occupancy grid into a simple image."""

    # The observed occupancy grid contains only:
    # - 0: outside/unknown
    # - 127: wall/obstacle
    # - 128: floor/free
    table = bytearray(range(256))
    table[0] = 0
    table[127] = 180
    table[128] = 255

    mapped = grid.translate(bytes(table))
    img = Image.frombytes("L", (size_x, size_y), mapped)
    img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM).convert("RGB")

    if room_boundaries:
        _draw_room_boundaries(img, room_boundaries)
    if room_labels:
        _draw_room_labels(img, room_labels, scale=1)

    if scale > 1:
        img = img.resize((size_x * scale, size_y * scale), resample=Image.Resampling.NEAREST)

    _draw_map_overlays(img, path=path, charger=charger, robot=robot, scale=max(1, scale))

    return img
