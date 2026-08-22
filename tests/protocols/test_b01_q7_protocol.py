"""Tests for B01/Q7 protocol helpers."""

from unittest.mock import Mock

from roborock.protocols import b01_q7_protocol
from roborock.protocols.b01_q7_protocol import MapKey
from roborock.roborock_message import RoborockMessage, RoborockMessageProtocol


def test_decode_live_map_message(monkeypatch) -> None:
    decoder = Mock(return_value=b"inflated-scmap")
    monkeypatch.setattr(b01_q7_protocol, "decode_map_payload", decoder)
    key = MapKey(b"0123456789abcdef")
    message = RoborockMessage(
        protocol=RoborockMessageProtocol.MAP_RESPONSE,
        version=b"B01",
        payload=b"encrypted-map",
    )

    assert b01_q7_protocol.decode_live_map_message(message, key) == b"inflated-scmap"
    decoder.assert_called_once_with(b"encrypted-map", key)


def test_decode_live_map_message_ignores_non_q7_map_messages(monkeypatch) -> None:
    decoder = Mock()
    monkeypatch.setattr(b01_q7_protocol, "decode_map_payload", decoder)
    key = MapKey(b"0123456789abcdef")

    assert (
        b01_q7_protocol.decode_live_map_message(
            RoborockMessage(protocol=RoborockMessageProtocol.RPC_RESPONSE, version=b"B01", payload=b"rpc"), key
        )
        is None
    )
    assert (
        b01_q7_protocol.decode_live_map_message(
            RoborockMessage(protocol=RoborockMessageProtocol.MAP_RESPONSE, version=b"1.0", payload=b"v1-map"), key
        )
        is None
    )
    assert (
        b01_q7_protocol.decode_live_map_message(
            RoborockMessage(protocol=RoborockMessageProtocol.MAP_RESPONSE, version=b"B01", payload=None), key
        )
        is None
    )
    decoder.assert_not_called()
