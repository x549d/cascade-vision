"""Exercise the packaged workspace and its local HTTP workflow."""

import http.client
import json
import threading
import time

import pytest

from cascade_vision.web import Job, ReviewServer


@pytest.fixture
def server():
    with ReviewServer(0) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=3)


def request(server, method, path, *, data=None, headers=None):
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        client.request(method, path, body=data, headers=headers or {})
        response = client.getresponse()
        return response.status, response.headers, response.read()
    finally:
        client.close()


def finished(server):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        _, _, data = request(server, "GET", "/api/state")
        job = json.loads(data)["job"]
        if job and job["status"] != "running":
            return job
        time.sleep(0.025)
    pytest.fail("Review job did not finish")


def test_offline_demo_delivers_events_images_and_downloads(server):
    status, _, page = request(server, "GET", "/")
    assert status == 200 and "视频分析工作台".encode() in page
    assert request(server, "GET", "/app.js")[0] == 200
    assert request(server, "POST", "/api/demo", headers={"X-Cascade-UI": "1"})[0] == 202
    job = finished(server)
    assert job["status"] == "done"
    assert job["summary"]["api_attempts"] == 0
    assert job["summary"]["metrics"]["total_frames"] == 300
    prefix = "/api/jobs/" + job["id"]
    _, _, raw_events = request(server, "GET", prefix + "/events")
    records = json.loads(raw_events)
    assert records and all(event["simulated"] for event in records)
    assert all(event["frame_target_count"] >= event["frame_person_count"] for event in records)
    assert any(event["frame_target_count"] > 1 for event in records)
    status, headers, image = request(server, "GET", prefix + "/frame?index=0")
    assert status == 200 and headers["Content-Type"] == "image/jpeg" and image.startswith(b"\xff\xd8")
    assert request(server, "GET", prefix + "/frame?index=10000")[0] == 404
    _, headers, exported = request(server, "GET", prefix + "/export")
    assert "attachment" in headers["Content-Disposition"]
    assert [json.loads(line) for line in exported.splitlines()] == records


def test_import_rejects_cross_origin_and_reports_corrupt_video(server):
    upload = {"X-Cascade-UI": "1", "Content-Type": "application/octet-stream"}
    path = "/api/run?name=bad.mp4"
    assert request(server, "POST", "/api/demo")[0] == 403
    assert request(server, "POST", path, data=b"bad", headers={**upload, "Origin": "https://example.com"})[0] == 403
    assert request(server, "POST", path + "&mode=api", data=b"bad", headers=upload)[0] == 400
    assert request(server, "POST", path, data=b"bad", headers=upload)[0] == 202
    job = finished(server)
    assert job["status"] == "error" and "无法打开视频" in job["error"]


def test_original_video_supports_byte_ranges(server, tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"0123456789")
    server.job = Job("clip.mp4", "local", 300, status="done", source=path)
    url = "/api/jobs/" + server.job.id + "/video"
    status, headers, data = request(server, "GET", url, headers={"Range": "bytes=3-6"})
    assert (status, headers["Content-Range"], data) == (206, "bytes 3-6/10", b"3456")
    assert request(server, "GET", url, headers={"Range": "bytes=-3"})[2] == b"789"
    assert request(server, "GET", url, headers={"Range": "bytes=100-"})[0] == 416
