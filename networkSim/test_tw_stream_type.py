#!/usr/bin/env python3
"""meta.json tw_stream_type (record only, 2026-10-25): MasterListener reads the twitch page's own GQL responses
(gql.twitch.tv/gql; the bodies via Network.getResponseBody, no request of ours) and records user.stream.type per
operation; the session's value is the first typed StreamMetadata read. Synthetic CDP messages, no network.

    python3 test_tw_stream_type.py        (exit 0 = pass)
"""
import json
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")
import browser as B  # noqa: E402

GQL = "https://gql.twitch.tv/gql"


def _drive(m, rid, sid, post, body):
    """One GQL request / response through handle() as CDP delivers it; returns the CDP commands sent."""
    sent = []

    def send(method, params, s=None):
        sent.append((method, params, s))
        return 1000 + len(sent)
    m.handle({"method": "Network.requestWillBeSent", "sessionId": sid,
              "params": {"requestId": rid, "request": {"url": GQL, "method": "POST", "postData": post}}}, send)
    m.handle({"method": "Network.responseReceived", "sessionId": sid,
              "params": {"requestId": rid, "response": {"url": GQL}}}, send)
    m.handle({"method": "Network.loadingFinished", "sessionId": sid, "params": {"requestId": rid}}, send)
    assert sent and sent[-1][0] == "Network.getResponseBody" and sent[-1][1] == {"requestId": rid} and sent[-1][2] == sid
    m.handle({"id": 1000 + len(sent), "result": {"body": body}}, send)
    return sent


def _meta(login, stream):
    return {"data": {"user": {"id": "1", "primaryColorHex": None, "lastBroadcast": {"id": "9", "title": "t"},
                              "stream": stream}},
            "extensions": {"durationMilliseconds": 12, "operationName": "StreamMetadata"}}


def test_recorded_from_the_pages_own_gql():
    m = B.MasterListener("ws://x")
    assert m.tw_stream_type()["value"] is None and m.tw_stream_type()["reason"] == "no GQL response body read"
    # a directory batch first: its streams carry a type but are not the channel's (data.game, not data.user)
    d_post = json.dumps([{"operationName": "DirectoryPage_Game", "variables": {"slug": "just-chatting"}}])
    d_body = json.dumps([{"data": {"game": {"streams": {"edges": [{"node": {"id": "5", "type": "rerun"}}]}}},
                          "extensions": {"operationName": "DirectoryPage_Game"}}])
    sent = _drive(m, "1", None, d_post, d_body)
    assert [x[0] for x in sent] == ["Network.getResponseBody"]          # reads the page's body: no request of ours
    assert m.gql_bodies == 1 and m.tw_stream_type()["reason"] == "no GQL response carried user.stream.type"
    # the channel page's batch: StreamMetadata (from a worker session) beside an unrelated operation
    post = json.dumps([{"operationName": "ChannelShell", "variables": {"login": "chan_a"}},
                       {"operationName": "StreamMetadata", "variables": {"channelLogin": "chan_a"}}])
    body = json.dumps([{"data": {"userOrError": {"id": "1", "login": "chan_a", "stream": {"id": "77", "viewersCount": 3}}},
                        "extensions": {"operationName": "ChannelShell"}},     # a stream without a type: not a source
                       _meta("chan_a", {"id": "77", "type": "live", "createdAt": "2026-10-25T00:00:00Z"})])
    _drive(m, "2", "W", post, body)
    r = m.tw_stream_type()
    assert (r["value"], r["source"], r["login"], r["stream_id"], r["reason"], r["changed"]) == \
        ("live", "gql:StreamMetadata", "chan_a", "77", None, False), r
    assert isinstance(r["t"], float) and r["reads_by_op"] == {"StreamMetadata": 1} and r["gql_bodies"] == 2
    # the same read again: counted, no new edge; then the stream turns into a rerun: an edge, changed, value kept
    _drive(m, "3", None, json.dumps({"operationName": "StreamMetadata", "variables": {"channelLogin": "chan_a"}}),
           json.dumps(_meta("chan_a", {"id": "77", "type": "live"})))
    _drive(m, "4", None, None, json.dumps(_meta(None, {"id": "78", "type": "rerun"})))   # no postData: op from extensions
    r = m.tw_stream_type()
    assert r["value"] == "live" and r["changed"] is True and r["reads_by_op"] == {"StreamMetadata": 3}
    assert [(e["op"], e["login"], e["stream_id"], e["value"]) for e in r["edges"]] == \
        [("StreamMetadata", "chan_a", "77", "live"), ("StreamMetadata", None, "78", "rerun")], r["edges"]
    json.dumps(r)                                                       # meta.json-serialisable
    assert "body" not in json.dumps(r["edges"])                         # no body kept


def test_absent_with_reason():
    m = B.MasterListener("ws://x")
    _drive(m, "1", None, json.dumps({"operationName": "StreamMetadata", "variables": {"channelLogin": "off"}}),
           json.dumps(_meta("off", None)))                              # offline: stream null
    r = m.tw_stream_type()
    assert r["value"] is None and r["source"] is None and r["reason"].startswith("user.stream was null"), r
    assert r["edges"][0]["value"] is None and r["edges"][0]["login"] == "off"
    m2 = B.MasterListener("ws://x")
    _drive(m2, "1", None, "{", "not json")                              # malformed: counted, never raised
    r2 = m2.tw_stream_type()
    assert r2["value"] is None and r2["errors"] == 1 and "JSONDecodeError" in r2["last_error"], r2
    m3 = B.MasterListener("ws://x")
    m3.error = "ConnectionRefusedError()"
    assert m3.tw_stream_type()["reason"] == "listener error: ConnectionRefusedError()"
    m4 = B.MasterListener("ws://x")                                     # an empty body (preflight / evicted): skipped
    _drive(m4, "1", None, None, "")
    assert m4.gql_bodies == 0 and m4.gql_errors == 0
    m5 = B.MasterListener("ws://x")                                     # the edge log's cap: counted, not silent
    m5.stream_type_edges.extend([{}] * B.WATCH_LOG_MAX)
    m5.stream_type_read(json.dumps(_meta("c", {"id": "1", "type": "live"})), 1.0)
    assert m5.stream_type_overflow == 1


def test_wired_into_meta():
    src = (HERE / "exp.py").read_text()
    assert "tw_stream_type=(browser._master.tw_stream_type()" in src
    bsrc = (HERE / "browser.py").read_text()
    assert bsrc.count('send("Network.getResponseBody"') == 1             # one body-read site, shared with the master / playlists


def main() -> int:
    test_recorded_from_the_pages_own_gql()
    test_absent_with_reason()
    test_wired_into_meta()
    print("test_tw_stream_type: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
