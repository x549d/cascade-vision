"""Loopback video review interface. Run with python -m cascade_vision.web."""

from __future__ import annotations

import argparse
import json
import logging
import math
import mimetypes
import re
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from itertools import islice
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import cv2

from .cli import generate_synthetic_stream
from .config import CascadeConfig
from .detector import CascadeTracker
from .pipeline import CascadePipeline

UPLOAD_LIMIT = 256 * 1024 * 1024
FRAME_LIMIT = 3600
MAX_ANALYSIS_SIDE = 1280
VIDEO_TYPES = {".mp4", ".m4v", ".mov", ".avi", ".webm", ".mkv"}
ASSETS = {"/": "index.html", "/app.css": "app.css", "/app.js": "app.js", "/icons.svg": "icons.svg"}
logger = logging.getLogger(__name__)


def analysis_frame(frame):
    height, width = frame.shape[:2]
    scale = min(1.0, MAX_ANALYSIS_SIDE / max(width, height))
    if scale == 1:
        return frame
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(frame, size, interpolation=cv2.INTER_AREA)


class ReviewPipeline(CascadePipeline):
    frame_size = None

    def process_frame(self, frame, render_hud=True, *, media_timestamp_s=None):
        frame = analysis_frame(frame)
        self.frame_size = [frame.shape[1], frame.shape[0]]
        return super().process_frame(frame, render_hud=render_hud, media_timestamp_s=media_timestamp_s)


@dataclass
class Job:
    name: str
    mode: str
    limit: int
    id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "running"
    processed: int = 0
    total: int = 0
    fps: float = 0.0
    source: Optional[Path] = None
    directory: Optional[tempfile.TemporaryDirectory] = None
    events: list = field(default_factory=list)
    event_frames: dict = field(default_factory=dict)
    summary: Optional[dict] = None
    error: str = ""
    detector_profile: str = "general"

    def snapshot(self):
        return {k: getattr(self, k) for k in ("id", "name", "mode", "status", "processed", "total", "summary", "error")}

    def image(self, index):
        event = self.events[index] if index >= 0 else {}
        position = max(0, event.get("frame_idx", 131 if self.mode == "demo" else 1) - 1)
        if self.source is None:
            frame = next(islice(generate_synthetic_stream(), position, None))
        else:
            capture = cv2.VideoCapture(str(self.source))
            try:
                capture.set(cv2.CAP_PROP_POS_FRAMES, position)
                ok, frame = capture.read()
            finally:
                capture.release()
            if not ok:
                raise ValueError("无法读取此时刻的截图。")
        frame = analysis_frame(frame)
        height, width = frame.shape[:2]
        scale = min(1, 960 / max(width, height))
        if scale < 1:
            frame = cv2.resize(frame, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
        for obj in self.event_frames.get(position + 1, ()):
            if obj["track_id"] != event.get("track_id"):
                x1, y1, x2, y2 = (round(v * scale) for v in obj["bbox"])
                cv2.rectangle(frame, (x1, y1), (x2, y2), (250, 194, 74), 2)
        if event.get("bbox"):
            x1, y1, x2, y2 = (round(v * scale) for v in event["bbox"])
            cv2.rectangle(frame, (x1, y1), (x2, y2), (126, 244, 99), 3)
        ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise ValueError("无法生成截图。")
        return data.tobytes()


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=8765, *, yolo_model=None, person_model=None, allow_api=False):
        super().__init__(("127.0.0.1", port), ReviewHandler)
        self.lock = threading.Lock()
        self.media_lock = threading.Lock()
        self.submission = threading.Lock()
        self.job = None
        self.worker = None
        self.yolo_model = yolo_model
        self.person_model = person_model
        self.allow_api = allow_api

    def api_available(self):
        return self.allow_api and bool(CascadeConfig.from_env().openai_api_key)

    def analyze(self, job, max_calls):
        events = []
        event_frames = {}
        counts = Counter()
        detected_frames = 0
        try:
            if job.source is None:
                source, fps, total = generate_synthetic_stream(), 30.0, 300
            else:
                capture = cv2.VideoCapture(str(job.source))
                try:
                    if not capture.isOpened():
                        raise ValueError("无法打开视频，请试用常见编码的 MP4 或 WebM 文件。")
                    fps = capture.get(cv2.CAP_PROP_FPS)
                    raw_total = capture.get(cv2.CAP_PROP_FRAME_COUNT)
                    total = int(raw_total) if math.isfinite(raw_total) and raw_total > 0 else 0
                finally:
                    capture.release()
                source = str(job.source)
            with self.lock:
                job.total = min(total, job.limit) if total else 0
                job.fps = fps if math.isfinite(fps) and fps > 0 else 0.0

            people = job.detector_profile == "person"
            weights = "mock" if job.mode == "demo" else self.person_model if people else self.yolo_model
            config = CascadeConfig.from_env(
                strategy="cascade",
                mock_vlm=job.mode != "api",
                yolo_model=weights,
                conf_min=0.25 if people else None,
                mock_latency_s=0.0 if job.mode != "api" else None,
                max_vlm_calls=max_calls,
                event_labels=["person", "backpack", "car", "bicycle", "motorcycle", "bus", "truck", "dog", "cat"],
            )
            if job.mode != "demo" and config.yolo_model.lower() in {"mock", "fallback", "synthetic"}:
                raise ValueError("导入视频需要真实 YOLO 权重，请检查模型配置。")
            if job.mode == "api" and not (self.allow_api and config.openai_api_key):
                raise ValueError("真实 AI 模式尚未在服务端启用。")
            tracker = CascadeTracker(config, person_only=True) if people else None
            pipeline = ReviewPipeline(config, tracker=tracker, event_callback=events.append)

            def step(report, stage):
                nonlocal detected_frames
                objects = stage.tracked_objects
                detected_frames += bool(objects)
                counts.update(obj.yolo_class for obj in objects)
                if stage.vlm_tasks:
                    event_frames[stage.frame_idx] = [obj.to_dict() for obj in objects]
                with self.lock:
                    job.processed = report.total_frames

            pipeline.run_stream(
                source, max_frames=job.limit, source_fps=fps if job.source is None else None, on_step=step
            )
            metrics = pipeline.get_metrics_summary()
            if metrics["total_frames"] == 0:
                raise ValueError("视频未解码出任何画面，请转换编码后重试。")
            ordered = sorted(events, key=lambda event: (event.get("frame_idx", 0), event.get("track_id", 0)))
            for event in ordered:
                frame_objects = event_frames.get(event.get("frame_idx"), ())
                event["frame_target_count"] = len(frame_objects)
                event["frame_person_count"] = sum(obj["yolo_class"] == "person" for obj in frame_objects)
            summary = {
                "source": job.name,
                "mode": job.mode,
                "mock_detector": job.mode == "demo",
                "mock_vlm": job.mode != "api",
                "detector_profile": job.detector_profile,
                "model": config.openai_model if job.mode == "api" else None,
                "source_frames": total or None,
                "analysis_size": pipeline.frame_size,
                "duration_s": (total or metrics["total_frames"]) / job.fps if job.fps else None,
                "processed_duration_s": metrics["total_frames"] / job.fps if job.fps else None,
                "limited": metrics["total_frames"] == job.limit and (total > job.limit or not total),
                "detected_frames": detected_frames,
                "detections_by_class": dict(counts.most_common()),
                "api_attempts": metrics["vlm_calls"] if job.mode == "api" else 0,
                "api_attempt_limit": max_calls if job.mode == "api" else None,
                "metrics": metrics,
            }
            with self.lock:
                job.events, job.event_frames, job.summary, job.status = ordered, event_frames, summary, "done"
        except (ImportError, ModuleNotFoundError):
            self.fail(job, "缺少 YOLO 依赖。请先安装项目的 [yolo] 可选依赖。")
        except (ValueError, FileNotFoundError) as exc:
            logger.warning("Review input or configuration failed: %s", type(exc).__name__)
            # Only display messages authored by the UI, not provider or filesystem details.
            message = str(exc)
            self.fail(
                job,
                message
                if message.startswith(("无法打开视频", "视频未解码", "导入视频需要", "真实 AI"))
                else "无法开始分析，请检查视频格式、YOLO 权重和运行配置。",
            )
        except Exception:
            logger.exception("Video review failed")
            self.fail(job, "分析未完成。请检查终端日志中的解码、模型或运行环境错误。")

    def fail(self, job, message):
        with self.lock:
            job.error, job.status = message, "error"

    def server_close(self):
        super().server_close()
        with self.submission:
            if self.worker:
                self.worker.join()
            with self.media_lock:
                if self.job and self.job.directory:
                    self.job.directory.cleanup()


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def setup(self):
        self.request.settimeout(30)
        super().setup()

    def allowed(self):
        port = self.server.server_port
        host = self.headers.get("Host", "")
        valid = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            valid.update(("127.0.0.1", "localhost"))
        origin = self.headers.get("Origin")
        if host not in valid or (origin and origin != f"http://{host}"):
            self.json({"error": "仅接受同源的本机请求。"}, 403)
            return False
        return True

    def respond(self, data, content_type, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionError, TimeoutError):
            pass

    def json(self, data, status=200, filename=None):
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.respond(raw, "application/json; charset=utf-8", status, filename)

    def do_GET(self):
        if not self.allowed():
            return
        url = urlsplit(self.path)
        if url.path in ASSETS:
            name = ASSETS[url.path]
            data = files("cascade_vision.web_assets").joinpath(name).read_bytes()
            kind = {
                "html": "text/html; charset=utf-8",
                "css": "text/css",
                "js": "text/javascript",
                "svg": "image/svg+xml",
            }[name.rsplit(".", 1)[1]]
            self.respond(data, kind)
            return
        if url.path == "/api/state":
            with self.server.lock:
                current = self.server.job.snapshot() if self.server.job else None
            self.json(
                {
                    "job": current,
                    "api_available": self.server.api_available(),
                    "person_model_available": bool(self.server.person_model),
                }
            )
            return
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/(events|summary|frame|video|export)", url.path)
        if not match:
            return self.json({"error": "页面不存在。"}, 404)
        job_id, action = match.groups()
        with self.server.media_lock:
            with self.server.lock:
                job = self.server.job
                valid = job and job.id == job_id and job.status == "done"
            if not valid:
                return self.json({"error": "当前结果尚未就绪或已被新任务替换。"}, 404)
            if action == "events":
                return self.json(job.events)
            if action == "summary":
                return self.json(job.summary, filename="cascade-vision-summary.json")
            if action == "export":
                lines = "".join(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n" for event in job.events)
                return self.respond(
                    lines.encode("utf-8"), "application/x-ndjson; charset=utf-8", filename="cascade-vision-events.jsonl"
                )
            if action == "video":
                if job.source is None:
                    return self.json({"error": "离线演示只提供截图。"}, 404)
                return self.video(job.source)
            try:
                index = int(parse_qs(url.query).get("index", ["-1"])[0])
                if not -1 <= index < len(job.events):
                    raise ValueError
                image = job.image(index)
            except (ValueError, cv2.error):
                return self.json({"error": "此截图不可用。"}, 404)
            return self.respond(image, "image/jpeg")

    def video(self, path):
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", requested)
            if match and any(match.groups()):
                first, last = match.groups()
                start = int(first) if first else max(0, size - int(last))
                end = min(end, int(last)) if first and last else end
            if not match or not any(match.groups()) or not 0 <= start <= end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "bytes")
        length = end - start + 1
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as file:
            file.seek(start)
            while length:
                data = file.read(min(length, 1024 * 1024))
                if not data:
                    break
                try:
                    self.wfile.write(data)
                except (ConnectionError, TimeoutError):
                    break
                length -= len(data)

    def do_POST(self):
        if not self.allowed():
            return
        if self.headers.get("X-Cascade-UI") != "1":
            return self.json({"error": "缺少本地界面请求标记。"}, 403)
        url = urlsplit(self.path)
        if url.path not in {"/api/demo", "/api/run"}:
            return self.json({"error": "页面不存在。"}, 404)
        if not self.server.submission.acquire(blocking=False):
            return self.json({"error": "正在导入另一段视频。"}, 409)
        try:
            with self.server.lock:
                busy = self.server.job and self.server.job.status == "running"
            if busy:
                return self.json({"error": "请等待当前分析完成。"}, 409)
            if url.path == "/api/demo":
                job, cap = Job("离线合成演示", "demo", 300), 0
            else:
                try:
                    job, cap = self.receive_video(parse_qs(url.query))
                except ValueError as exc:
                    return self.json({"error": str(exc)}, 400)
                except OSError:
                    return self.json({"error": "无法保存视频，请检查本机磁盘空间后重试。"}, 400)
            with self.server.media_lock, self.server.lock:
                old = self.server.job
                if old and old.directory:
                    old.directory.cleanup()
                self.server.job = job
            self.server.worker = threading.Thread(target=self.server.analyze, args=(job, cap), daemon=True)
            self.server.worker.start()
            self.json({"id": job.id}, 202)
        finally:
            self.server.submission.release()

    def receive_video(self, query):
        def value(key, default):
            return query.get(key, [default])[0]

        name = value("name", "").replace("\\", "/").rsplit("/", 1)[-1][:240]
        suffix = Path(name).suffix.lower()
        if suffix not in VIDEO_TYPES:
            raise ValueError("请选择 MP4、MOV、AVI、WebM 或 MKV 视频。")
        if self.headers.get_content_type() != "application/octet-stream" or self.headers.get("Transfer-Encoding"):
            raise ValueError("上传格式不受支持。")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            limit = int(value("limit", str(FRAME_LIMIT)))
            cap = int(value("cap", "10"))
        except ValueError as exc:
            raise ValueError("文件大小、帧数或请求上限无效。") from exc
        if not 0 < size <= UPLOAD_LIMIT:
            raise ValueError("请选择不超过 256 MiB 的非空视频。")
        if not 1 <= limit <= FRAME_LIMIT:
            raise ValueError(f"每次最多处理 {FRAME_LIMIT} 帧。")
        mode = value("mode", "local")
        profile = value("detector", "general")
        if profile not in {"general", "person"} or (profile == "person" and not self.server.person_model):
            raise ValueError("所选检测模型未配置。")
        if mode not in {"local", "api"}:
            raise ValueError("分析模式无效。")
        if mode == "api" and not (1 <= cap <= 50 and self.server.api_available() and value("consent", "") == "1"):
            raise ValueError("真实 AI 分析需要在服务端启用，设置 1–50 次请求上限并确认提交。")
        directory = tempfile.TemporaryDirectory(prefix="cascade-review-")
        path = Path(directory.name) / ("input" + suffix)
        try:
            with path.open("wb") as file:
                while size:
                    chunk = self.rfile.read(min(size, 1024 * 1024))
                    if not chunk:
                        raise ValueError("视频传输中断，请重试。")
                    file.write(chunk)
                    size -= len(chunk)
        except BaseException:
            directory.cleanup()
            raise
        return Job(name, mode, limit, source=path, directory=directory, detector_profile=profile), (
            cap if mode == "api" else 0
        )

    def log_message(self, fmt, *args):
        # Avoid logging user filenames from query strings or flooding the terminal during polling.
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--yolo-model", help="Use a local YOLO weight file, or the configured default.")
    parser.add_argument("--person-model", type=Path, help="Offer experimental, local person-only weights in the UI.")
    parser.add_argument("--allow-api", action="store_true", help="Offer paid model requests with explicit UI opt-in.")
    args = parser.parse_args()
    if args.person_model and (args.person_model.suffix.lower() != ".pt" or not args.person_model.is_file()):
        parser.error("--person-model must be a local .pt file")
    try:
        server = ReviewServer(
            args.port,
            yolo_model=args.yolo_model,
            person_model=str(args.person_model.resolve()) if args.person_model else None,
            allow_api=args.allow_api,
        )
    except OSError as exc:
        parser.exit(1, f"Unable to start local interface: {exc}\n")
    with server:
        print(f"Cascade Vision: http://127.0.0.1:{server.server_port} (Ctrl+C to stop)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
