import json

from model_gateway import decode_sse, encode_sse
from model_gateway.sse import parse_sse


def test_encode_and_decode_roundtrip():
    frame = encode_sse("content_delta", {"text": "你好"})
    assert frame.endswith("\n\n")
    event, data = decode_sse(frame)
    assert event == "content_delta"
    assert data == {"text": "你好"}


def test_parse_sse_comments_multiline_and_crlf():
    lines = [
        ": keep-alive",
        "event: content_block_delta",
        "data: {\"text\":",
        "data: \"Hi\"}",
        "",
        "data: [DONE]",
        "",
    ]
    events = list(parse_sse(line.replace("\n", "") for line in lines))
    # 上面的 data 行本身不含真实换行拆分，改用显式行。
    raw = "\r\n".join(
        [
            ": ping",
            "event: content_block_delta",
            'data: {"part":',
            'data: "one"}',
            "",
            "data: [DONE]",
            "",
        ]
    )
    parsed = list(parse_sse(raw.splitlines()))
    assert parsed[0][0] == "content_block_delta"
    assert json.loads(parsed[0][1]) == {"part": "one"}
    assert parsed[1] == ("message", "[DONE]")
    assert events
