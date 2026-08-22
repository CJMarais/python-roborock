from unittest.mock import patch

import pytest
from vacuum_map_parser_base.map_data import MapData

from roborock.devices.traits.b01.q7 import Q7PropertiesApi
from roborock.exceptions import RoborockException
from roborock.map.b01_map_parser import ParsedMapData
from roborock.roborock_typing import RoborockB01Q7Methods

from .conftest import FakeQ7Channel


async def test_q7_map_content_refresh_populates_cached_values(
    q7_api: Q7PropertiesApi,
    fake_channel: FakeQ7Channel,
):
    fake_channel.response_queue.extend(
        [
            {"map_list": [{"id": 1772093512, "cur": True}]},
            b"inflated-payload",
        ]
    )

    # Ensure we have map metadata first
    await q7_api.map.refresh()

    dummy_map_data = MapData()
    parsed_map_data = ParsedMapData(
        image_content=b"pngbytes",
        map_data=dummy_map_data,
    )
    with patch(
        "roborock.devices.traits.b01.q7.map_content.B01MapParser.parse",
        return_value=parsed_map_data,
    ) as parse:
        await q7_api.map_content.refresh()

    assert q7_api.map_content.image_content == b"pngbytes"
    assert q7_api.map_content.map_data is dummy_map_data
    assert q7_api.map_content.raw_api_response == b"inflated-payload"

    parse.assert_called_once_with(b"inflated-payload")

    assert len(fake_channel.published_commands) == 2
    cmd, params = fake_channel.published_commands[0]
    assert cmd == RoborockB01Q7Methods.GET_MAP_LIST

    map_cmd, map_params = fake_channel.published_commands[1]
    assert map_cmd == RoborockB01Q7Methods.UPLOAD_BY_MAPID
    assert map_params == {"map_id": 1772093512}


def test_q7_live_map_updates_cache_and_notifies_without_retaining_raw(
    q7_api: Q7PropertiesApi,
):
    """A pushed map updates consumers but does not retain decrypted protobuf bytes."""
    updates: list[None] = []
    q7_api.map_content.add_update_listener(lambda: updates.append(None))
    dummy_map_data = MapData()
    parsed_map_data = ParsedMapData(image_content=b"live-png", map_data=dummy_map_data)

    with patch(
        "roborock.devices.traits.b01.q7.map_content.B01MapParser.parse",
        return_value=parsed_map_data,
    ) as parse:
        q7_api.map_content.update_from_live_map(b"live-inflated-map")

    parse.assert_called_once_with(b"live-inflated-map")
    assert q7_api.map_content.image_content == b"live-png"
    assert q7_api.map_content.map_data is dummy_map_data
    assert q7_api.map_content.raw_api_response is None
    assert updates == [None]


def test_q7_static_poll_does_not_replace_active_live_progress(q7_api: Q7PropertiesApi):
    """Interleaved request responses must not clear a changing live map."""
    live_map_data = MapData()
    live_map_data.path = object()  # type: ignore[assignment]
    live_map_data.vacuum_position = object()  # type: ignore[assignment]
    static_map_data = MapData()
    parsed_maps = [
        ParsedMapData(image_content=b"live-1", map_data=live_map_data),
        ParsedMapData(image_content=b"static", map_data=static_map_data),
        ParsedMapData(image_content=b"live-2", map_data=live_map_data),
    ]
    updates: list[None] = []
    q7_api.map_content.add_update_listener(lambda: updates.append(None))

    with patch(
        "roborock.devices.traits.b01.q7.map_content.B01MapParser.parse",
        side_effect=parsed_maps,
    ):
        assert q7_api.map_content.update_from_live_map(b"live-1") is True
        assert q7_api.map_content.update_from_live_map(b"static") is False
        assert q7_api.map_content.image_content == b"live-1"
        assert q7_api.map_content.update_from_live_map(b"live-2") is True

    assert q7_api.map_content.image_content == b"live-2"
    assert updates == [None, None]


def test_q7_session_reset_allows_static_map_then_new_live_progress(q7_api: Q7PropertiesApi):
    """A new session boundary releases protection from the previous path."""
    live_map_data = MapData()
    live_map_data.path = object()  # type: ignore[assignment]
    live_map_data.vacuum_position = object()  # type: ignore[assignment]
    static_map_data = MapData()
    parsed_maps = [
        ParsedMapData(image_content=b"old-live", map_data=live_map_data),
        ParsedMapData(image_content=b"new-static", map_data=static_map_data),
        ParsedMapData(image_content=b"new-live", map_data=live_map_data),
    ]

    with patch(
        "roborock.devices.traits.b01.q7.map_content.B01MapParser.parse",
        side_effect=parsed_maps,
    ):
        q7_api.map_content.update_from_live_map(b"old-live")
        q7_api.map_content.reset_live_progress_session()
        assert q7_api.map_content.update_from_live_map(b"new-static") is True
        assert q7_api.map_content.image_content == b"new-static"
        assert q7_api.map_content.update_from_live_map(b"new-live") is True

    assert q7_api.map_content.image_content == b"new-live"


async def test_q7_map_content_refresh_falls_back_to_first_map(
    q7_api: Q7PropertiesApi,
    fake_channel: FakeQ7Channel,
):
    """If no current map marker exists, first map in list is used."""
    fake_channel.response_queue.extend(
        [
            {"map_list": [{"id": 111}, {"id": 222, "cur": False}]},
            b"inflated-payload",
        ]
    )

    # Load current map
    await q7_api.map.refresh()

    dummy_map_data = MapData()
    with patch(
        "roborock.devices.traits.b01.q7.map_content.B01MapParser.parse",
        return_value=type("X", (), {"image_content": b"pngbytes", "map_data": dummy_map_data})(),
    ):
        await q7_api.map_content.refresh()

    assert len(fake_channel.published_commands) == 2
    map_cmd, map_params = fake_channel.published_commands[1]
    assert map_params == {"map_id": 111}


async def test_q7_map_content_refresh_errors_without_map_list(
    q7_api: Q7PropertiesApi,
    fake_channel: FakeQ7Channel,
):
    """Refresh should fail clearly when map list is unusable."""
    fake_channel.response_queue.extend([{"map_list": []}])

    with pytest.raises(RoborockException, match="Unable to determine current map ID"):
        await q7_api.map_content.refresh()
