"""기관별 A2A 어댑터가 공통으로 쓰는 protobuf 변환 도우미입니다."""

from __future__ import annotations

from typing import Any

from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp

from a2a.types.a2a_pb2 import Part


def dict_to_data_part(data: dict[str, Any]) -> Part:
    # Python 사전을 A2A의 구조화된 DataPart로 바꿉니다.
    value = ParseDict(data, Value())
    return Part(data=value)


def part_to_data(part: Part) -> dict[str, Any] | None:
    # 텍스트 Part는 여기서 처리하지 않으며 호출자가 별도로 해석합니다.
    if part.WhichOneof("content") != "data":
        return None
    value = MessageToDict(part.data)
    return value if isinstance(value, dict) else {"value": value}


def timestamp_now() -> Timestamp:
    timestamp = Timestamp()
    timestamp.GetCurrentTime()
    return timestamp
