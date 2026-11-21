"""CSO stdio v1: big-endian 요청 ID와 길이를 사용하는 UTF-8 프레임."""

import struct

READY = b"AIVAX_CSO_V1\n"
HEADER = struct.Struct(">QI")
LENGTH = struct.Struct(">I")
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_FILES = 65536


def encode_request(request_id: int, paths: tuple[str, ...]) -> bytes:
    if len(paths) > MAX_FILES:
        raise ValueError("CSO 요청 파일 수가 제한을 초과했습니다.")
    frame = bytearray(HEADER.pack(request_id, len(paths)))
    for path in paths:
        encoded = path.encode("utf-8")
        if len(frame) + LENGTH.size + len(encoded) > MAX_FRAME_BYTES:
            raise ValueError("CSO 요청 크기가 제한을 초과했습니다.")
        frame.extend(LENGTH.pack(len(encoded)))
        frame.extend(encoded)
    return bytes(frame)
