from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import h200_recordings as h200


ROOT = Path(__file__).resolve().parent
MEDIA_DIR = ROOT / "recordings"
HUB_CONNECT_RETRIES = 3
HUB_CONNECT_RETRY_DELAY = 1.5


@dataclass
class AppState:
    config_path: str
    key_file: str
    host: str | None = None
    user: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)


STATE: AppState


def api_args() -> argparse.Namespace:
    return argparse.Namespace(
        host=STATE.host,
        user=STATE.user,
        config=STATE.config_path,
        key_file=STATE.key_file,
    )


def get_credentials() -> h200.Credentials:
    return h200.prompt_credentials(api_args())


def get_hub_and_camera() -> tuple[h200.Credentials, Any, dict[str, Any]]:
    creds = get_credentials()
    last_error: Exception | None = None
    for attempt in range(1, HUB_CONNECT_RETRIES + 1):
        try:
            ok, error = h200.can_connect(creds.host, 443, timeout=3.0)
            if not ok:
                raise RuntimeError(f"H200 {creds.host}:443 연결 실패: {error}")
            hub = h200.connect_hub(creds)
            cameras = h200.list_paired_cameras(hub)
            break
        except Exception as err:
            last_error = err
            if attempt == HUB_CONNECT_RETRIES:
                raise RuntimeError(
                    "H200 연결 실패. H200 전원/네트워크/IP를 확인한 뒤 새로고침하세요. "
                    f"host={creds.host}:443 error={err}"
                ) from err
            time.sleep(HUB_CONNECT_RETRY_DELAY)
    else:
        raise RuntimeError(f"H200 연결 실패: {last_error}")
    if not cameras:
        raise RuntimeError("No paired cameras returned by H200.")
    camera = h200.pick_camera(cameras, argparse.Namespace(camera_index=None))
    return creds, hub, camera


def parse_date(value: str | None) -> str:
    if not value:
        return h200.local_today()
    value = value.strip()
    if "-" in value:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y%m%d")
    return datetime.strptime(value, "%Y%m%d").strftime("%Y%m%d")


def date_for_input(value: str) -> str:
    return datetime.strptime(value, "%Y%m%d").strftime("%Y-%m-%d")


def clip_filename(camera: dict[str, Any], start_time: int) -> str:
    path = h200.output_path_for_clip(str(MEDIA_DIR), camera, start_time, "mp4")
    return path.relative_to(MEDIA_DIR).as_posix()


def legacy_utc_clip_filename(camera: dict[str, Any], start_time: int) -> str:
    timestamp = datetime.fromtimestamp(start_time, tz=timezone.utc).strftime(
        "%Y%m%d_%H%M%S",
    )
    date_dir = datetime.fromtimestamp(start_time).astimezone().strftime("%Y%m%d")
    alias = (camera.get("alias") or "d230").replace(" ", "_")
    return f"{date_dir}/{alias}_{timestamp}.mp4"


def existing_clip_path(relative_names: list[str]) -> tuple[Path, str]:
    for relative_name in relative_names:
        path = MEDIA_DIR / relative_name
        if path.exists() and path.stat().st_size > 0:
            return path, relative_name

        legacy_path = MEDIA_DIR / Path(relative_name).name
        if legacy_path.exists() and legacy_path.stat().st_size > 0:
            return legacy_path, legacy_path.name

    return MEDIA_DIR / relative_names[0], relative_names[0]


def clip_to_json(camera: dict[str, Any], clip: dict[str, Any]) -> dict[str, Any]:
    start_time = int(clip["startTime"])
    end_time = int(clip["endTime"])
    filename = clip_filename(camera, start_time)
    mp4_path, play_filename = existing_clip_path(
        [filename, legacy_utc_clip_filename(camera, start_time)],
    )
    downloaded = mp4_path.exists() and mp4_path.stat().st_size > 0
    return {
        "startTime": start_time,
        "endTime": end_time,
        "duration": end_time - start_time,
        "localStart": datetime.fromtimestamp(start_time).astimezone().isoformat(),
        "localTime": datetime.fromtimestamp(start_time).astimezone().strftime("%H:%M:%S"),
        "filename": filename,
        "downloaded": downloaded,
        "size": mp4_path.stat().st_size if downloaded else 0,
        "mp4Ready": downloaded,
        "playUrl": f"/media/{quote(play_filename)}",
    }


def list_clips(date: str) -> dict[str, Any]:
    creds, hub, camera = get_hub_and_camera()
    clips = h200.list_recordings(hub, camera, date, date)
    return {
        "date": date_for_input(date),
        "host": creds.host,
        "camera": {
            "alias": camera.get("alias"),
            "model": camera.get("device_model"),
            "deviceId": camera.get("device_id"),
            "mac": camera.get("mac"),
        },
        "clips": [clip_to_json(camera, clip) for clip in clips],
    }


def media_path(name: str) -> Path:
    relative = Path(unquote(name))
    if relative.is_absolute() or ".." in relative.parts:
        raise FileNotFoundError(name)

    path = MEDIA_DIR / relative
    if not path.exists() or path.stat().st_size == 0:
        path = MEDIA_DIR / relative.name
    if not path.exists() or path.stat().st_size == 0:
        raise FileNotFoundError(path)
    return path


def run_download_job(job_id: str, date: str, skip_existing: bool) -> None:
    def update(**kwargs: Any) -> None:
        with STATE.lock:
            STATE.jobs[job_id].update(kwargs)

    try:
        update(status="running", message="Connecting to H200...")
        creds, hub, camera = get_hub_and_camera()
        clips = h200.list_recordings(hub, camera, date, date)
        camera_client = h200.connect_camera(creds, camera)
        update(total=len(clips), downloaded=0, skipped=0, failed=0)
        for idx, clip in enumerate(clips):
            start_time = int(clip["startTime"])
            end_time = int(clip["endTime"])
            output = h200.output_path_for_clip(str(MEDIA_DIR), camera, start_time, "mp4")
            if h200.should_skip_output(output, skip_existing):
                update(
                    current=idx + 1,
                    skipped=STATE.jobs[job_id]["skipped"] + 1,
                    message=f"Skipped {output.name}",
                )
                continue

            update(current=idx + 1, message=f"Downloading {output.name}")
            ok = asyncio.run(
                h200.download_recording(
                    camera_client,
                    start_time,
                    end_time,
                    output,
                    window_size=50,
                    stall_timeout=10.0,
                    output_format="mp4",
                    keep_ts=False,
                )
            )
            with STATE.lock:
                if ok:
                    STATE.jobs[job_id]["downloaded"] += 1
                else:
                    STATE.jobs[job_id]["failed"] += 1
        update(status="done", message="Done")
    except Exception as err:
        update(status="error", message=str(err))


INDEX_HTML = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>H200 Recordings</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.css">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/themes/dark.css">
  <style>
    :root {
      color-scheme: dark;
      --bg: #101214;
      --panel: #171b1f;
      --panel2: #12161a;
      --line: #293038;
      --text: #eef2f6;
      --muted: #9aa7b4;
      --accent: #2dd4bf;
      --accent2: #fbbf24;
      --danger: #fb7185;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, input { font: inherit; }
    .app {
      display: grid;
      grid-template-columns: 270px minmax(0, 1fr);
      min-height: 100vh;
    }
    aside {
      border-right: 1px solid var(--line);
      background: var(--panel);
      padding: 18px;
    }
    main {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      min-width: 0;
      min-height: 100vh;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--panel2);
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 700;
      letter-spacing: 0;
    }
    .status {
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .datebar {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .dateInput {
      min-width: 0;
      width: 180px;
      border: 1px solid var(--line);
      background: #11161b;
      color: var(--text);
      border-radius: 6px;
      min-height: 38px;
      padding: 0 10px;
    }
    .dateInput::-webkit-calendar-picker-indicator {
      filter: invert(1);
      opacity: .8;
    }
    .flatpickr-calendar {
      border: 1px solid var(--line);
      box-shadow: 0 10px 26px rgb(0 0 0 / .35);
    }
    .iconbtn, .cmd {
      border: 1px solid var(--line);
      background: #20262d;
      color: var(--text);
      border-radius: 6px;
      min-height: 38px;
      cursor: pointer;
    }
    .iconbtn {
      width: 34px;
      display: inline-grid;
      place-items: center;
      font-size: 20px;
      line-height: 1;
    }
    .cmd {
      padding: 0 13px;
      font-weight: 650;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      text-decoration: none;
    }
    .cmd.primary {
      background: #123c38;
      border-color: #1d766c;
      color: #c8fffa;
    }
    .cmd:disabled, .iconbtn:disabled {
      opacity: .45;
      cursor: wait;
    }
    .controls {
      display: flex;
      gap: 8px;
      margin-top: 14px;
    }
    .controls .cmd { flex: 1; }
    .content {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      min-height: 0;
    }
    .list {
      overflow: auto;
      background: #111417;
      border-top: 1px solid var(--line);
    }
    .clip {
      width: 100%;
      text-align: left;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      padding: 13px 16px;
      border: 0;
      border-bottom: 1px solid var(--line);
      background: transparent;
      color: var(--text);
      cursor: pointer;
    }
    .clip:hover, .clip.active { background: #1b2229; }
    .clip strong { display: block; font-size: 16px; }
    .clip span { color: var(--muted); font-size: 13px; }
    .badge {
      align-self: center;
      border-radius: 999px;
      border: 1px solid var(--line);
      color: var(--muted);
      padding: 4px 8px;
      font-size: 12px;
    }
    .badge.ok {
      color: #b6fff5;
      border-color: #176c64;
      background: #0f2f2c;
    }
    .player {
      min-width: 0;
      padding: 16px 18px;
      background: #0e1114;
    }
    video {
      width: 100%;
      max-height: min(58vh, 680px);
      background: black;
      border: 1px solid var(--line);
      border-radius: 6px;
      display: block;
    }
    .empty {
      color: var(--muted);
      padding: 24px;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .playerActions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }
    .playerActions[hidden] { display: none; }
    @media (min-width: 768px) and (max-width: 1180px) {
      .app { grid-template-columns: 1fr; }
      aside {
        position: sticky;
        top: 0;
        z-index: 5;
        display: flex;
        align-items: center;
        gap: 10px 14px;
        border-right: 0;
        border-bottom: 1px solid var(--line);
        padding: 12px 14px;
      }
      .datebar { flex: 0 0 auto; }
      .dateInput {
        width: 180px;
        margin-top: 0;
      }
      .controls {
        flex: 1 1 260px;
        max-width: 340px;
        margin-left: auto;
        margin-top: 0;
      }
      main { min-height: calc(100dvh - 150px); }
      .topbar { padding: 12px 14px; }
      .player { padding: 12px 14px; }
      video { max-height: 54dvh; }
      .list {
        max-height: 32dvh;
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        align-content: start;
      }
      .clip:nth-child(odd) { border-right: 1px solid var(--line); }
      .empty { grid-column: 1 / -1; }
    }
    @media (max-width: 767px) {
      .app { grid-template-columns: 1fr; }
      aside {
        border-right: 0;
        border-bottom: 1px solid var(--line);
        padding: 12px;
      }
      .datebar { width: 100%; }
      .dateInput { flex: 1; width: auto; }
      .topbar { align-items: flex-start; flex-direction: column; }
      .player { padding: 12px; }
      .list { max-height: 38vh; }
      video { max-height: 46vh; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <div class="datebar">
        <button class="iconbtn" id="prevDay" title="이전 날">‹</button>
        <input class="dateInput" id="dateInput" type="date" title="날짜 선택">
        <button class="iconbtn" id="nextDay" title="다음 날">›</button>
      </div>
      <div class="controls">
        <button class="cmd" id="refreshBtn">새로고침</button>
        <button class="cmd primary" id="downloadBtn">다운로드</button>
      </div>
    </aside>
    <main>
      <div class="topbar">
        <h1 id="dateTitle"></h1>
        <div class="status" id="status">대기 중</div>
      </div>
      <div class="content">
        <section class="player">
          <video id="video" controls playsinline preload="auto"></video>
          <div class="playerActions" id="playerActions" hidden>
            <a class="cmd primary" id="deviceDownloadLink" href="#">기기에 다운로드</a>
          </div>
          <div class="meta" id="meta"></div>
        </section>
        <section class="list" id="clipList"></section>
      </div>
    </main>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.js"></script>
  <script>
    const pad = n => String(n).padStart(2, "0");
    const fmtDate = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
    const parseLocal = s => {
      const [y,m,d] = s.split("-").map(Number);
      return new Date(y, m - 1, d);
    };

    let selected = fmtDate(new Date());
    let clips = [];
    let activeFile = "";
    let jobTimer = null;
    let activeObjectUrl = "";
    let playLoadId = 0;
    let datePicker = null;

    const dateInput = document.getElementById("dateInput");
    const dateTitle = document.getElementById("dateTitle");
    const statusEl = document.getElementById("status");
    const clipList = document.getElementById("clipList");
    const video = document.getElementById("video");
    const meta = document.getElementById("meta");
    const playerActions = document.getElementById("playerActions");
    const deviceDownloadLink = document.getElementById("deviceDownloadLink");
    const refreshBtn = document.getElementById("refreshBtn");
    const downloadBtn = document.getElementById("downloadBtn");

    function setBusy(busy) {
      refreshBtn.disabled = busy;
      downloadBtn.disabled = busy;
    }

    function setStatus(text) {
      statusEl.textContent = text;
    }

    function renderDateInput() {
      dateInput.value = selected;
      if (datePicker) datePicker.setDate(selected, false);
    }

    function setSelectedDate(value) {
      selected = value;
      renderDateInput();
      loadDate();
    }

    function moveSelectedDate(days) {
      const next = parseLocal(selected);
      next.setDate(next.getDate() + days);
      setSelectedDate(fmtDate(next));
    }

    function renderClips() {
      dateTitle.textContent = selected;
      clipList.innerHTML = "";
      if (!clips.length) {
        clearVideoObjectUrl();
        playerActions.hidden = true;
        clipList.innerHTML = '<div class="empty">목록 없음</div>';
        video.removeAttribute("src");
        video.load();
        meta.textContent = "";
        return;
      }
      for (const clip of clips) {
        const btn = document.createElement("button");
        btn.className = "clip";
        if (clip.filename === activeFile) btn.classList.add("active");
        btn.innerHTML = `
          <div>
            <strong>${clip.localTime}</strong>
            <span>${clip.duration}s · ${formatBytes(clip.size)}</span>
          </div>
          <div class="badge ${clip.downloaded ? "ok" : ""}">${clip.downloaded ? "저장됨" : "H200"}</div>
        `;
        btn.onclick = () => playClip(clip);
        clipList.appendChild(btn);
      }
    }

    function clearVideoObjectUrl() {
      if (activeObjectUrl) {
        URL.revokeObjectURL(activeObjectUrl);
        activeObjectUrl = "";
      }
    }

    function formatBytes(bytes) {
      if (!bytes) return "0 B";
      const units = ["B","KB","MB","GB"];
      let value = bytes;
      let idx = 0;
      while (value >= 1024 && idx < units.length - 1) {
        value /= 1024;
        idx++;
      }
      return `${value.toFixed(idx ? 1 : 0)} ${units[idx]}`;
    }

    async function loadDate() {
      setBusy(true);
      setStatus("목록 조회 중");
      try {
        const res = await fetch(`/api/list?date=${selected}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "목록 조회 실패");
        clips = data.clips || [];
        activeFile = "";
        renderClips();
        setStatus(`${clips.length}개`);
      } catch (err) {
        clips = [];
        renderClips();
        setStatus(err.message);
      } finally {
        setBusy(false);
      }
    }

    async function downloadAll() {
      setBusy(true);
      setStatus("다운로드 시작");
      try {
        const res = await fetch("/api/download_all", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({date: selected, skipExisting: true})
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "다운로드 시작 실패");
        pollJob(data.jobId);
      } catch (err) {
        setStatus(err.message);
        setBusy(false);
      }
    }

    async function pollJob(jobId) {
      clearTimeout(jobTimer);
      const res = await fetch(`/api/job?id=${jobId}`);
      const job = await res.json();
      const total = job.total || 0;
      setStatus(`${job.message || job.status} · ${job.current || 0}/${total} · 저장 ${job.downloaded || 0} · 건너뜀 ${job.skipped || 0}`);
      if (job.status === "done") {
        setBusy(false);
        await loadDate();
        return;
      }
      if (job.status === "error") {
        setBusy(false);
        return;
      }
      jobTimer = setTimeout(() => pollJob(jobId), 1200);
    }

    async function playClip(clip) {
      activeFile = clip.filename;
      renderClips();
      if (!clip.downloaded) {
        setStatus("먼저 다운로드 필요");
        return;
      }
      const loadId = ++playLoadId;
      meta.textContent = `${clip.localTime} · ${clip.duration}s · ${formatBytes(clip.size)}`;
      const downloadName = clip.filename.split("/").pop();
      deviceDownloadLink.href = `${clip.playUrl}?download=1`;
      deviceDownloadLink.download = downloadName;
      playerActions.hidden = false;
      video.oncanplay = () => setStatus("재생 가능");
      video.onwaiting = () => setStatus("버퍼링 중");
      video.onplaying = () => setStatus("재생 중");
      video.onerror = () => setStatus("재생 실패");
      setStatus("영상 불러오는 중");

      try {
        const res = await fetch(clip.playUrl);
        if (!res.ok) throw new Error("영상 로드 실패");
        const total = Number(res.headers.get("Content-Length")) || clip.size || 0;
        const reader = res.body && res.body.getReader ? res.body.getReader() : null;
        let blob;
        if (reader) {
          const chunks = [];
          let loaded = 0;
          while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            if (loadId !== playLoadId) return;
            chunks.push(value);
            loaded += value.length;
            if (total) {
              setStatus(`영상 불러오는 중 ${Math.round((loaded / total) * 100)}%`);
            }
          }
          blob = new Blob(chunks, {type: res.headers.get("Content-Type") || "video/mp4"});
        } else {
          blob = await res.blob();
        }
        if (loadId !== playLoadId) return;

        clearVideoObjectUrl();
        activeObjectUrl = URL.createObjectURL(blob);
        video.src = activeObjectUrl;
        video.load();
        setStatus("재생 준비 중");
      } catch (err) {
        setStatus(err.message);
      }
    }

    document.getElementById("prevDay").onclick = () => moveSelectedDate(-1);
    document.getElementById("nextDay").onclick = () => moveSelectedDate(1);
    dateInput.onchange = () => {
      if (!dateInput.value) return;
      setSelectedDate(dateInput.value);
    };
    if (window.flatpickr) {
      datePicker = flatpickr(dateInput, {
        allowInput: true,
        dateFormat: "Y-m-d",
        defaultDate: selected,
        disableMobile: false,
        onChange: (_dates, dateStr) => {
          if (dateStr && dateStr !== selected) setSelectedDate(dateStr);
        }
      });
    }
    refreshBtn.onclick = loadDate;
    downloadBtn.onclick = downloadAll;

    renderDateInput();
    loadDate();
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "H200Web/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, content_type: str = "text/html; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.send_text(INDEX_HTML)
            elif parsed.path == "/api/list":
                qs = parse_qs(parsed.query)
                date = parse_date(qs.get("date", [None])[0])
                self.send_json(list_clips(date))
            elif parsed.path == "/api/job":
                qs = parse_qs(parsed.query)
                job_id = qs.get("id", [""])[0]
                with STATE.lock:
                    job = STATE.jobs.get(job_id)
                if not job:
                    self.send_json({"error": "job not found"}, 404)
                else:
                    self.send_json(job)
            elif parsed.path.startswith("/media/"):
                name = parsed.path.removeprefix("/media/")
                qs = parse_qs(parsed.query)
                as_download = qs.get("download", ["0"])[0] == "1"
                self.send_file(
                    media_path(name),
                    download_name=Path(unquote(name)).name if as_download else None,
                )
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as err:
            self.send_json({"error": str(err)}, 500)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                body = INDEX_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
            elif parsed.path.startswith("/media/"):
                name = parsed.path.removeprefix("/media/")
                qs = parse_qs(parsed.query)
                as_download = qs.get("download", ["0"])[0] == "1"
                self.send_file(
                    media_path(name),
                    head_only=True,
                    download_name=Path(unquote(name)).name if as_download else None,
                )
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as err:
            self.send_json({"error": str(err)}, 500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/download_all":
                payload = self.read_json()
                date = parse_date(payload.get("date"))
                skip_existing = bool(payload.get("skipExisting", True))
                job_id = uuid.uuid4().hex
                with STATE.lock:
                    STATE.jobs[job_id] = {
                        "id": job_id,
                        "status": "queued",
                        "message": "Queued",
                        "date": date_for_input(date),
                        "total": 0,
                        "current": 0,
                        "downloaded": 0,
                        "skipped": 0,
                        "failed": 0,
                        "createdAt": time.time(),
                    }
                thread = threading.Thread(
                    target=run_download_job,
                    args=(job_id, date, skip_existing),
                    daemon=True,
                )
                thread.start()
                self.send_json({"jobId": job_id})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as err:
            self.send_json({"error": str(err)}, 500)

    def send_file(
        self,
        path: Path,
        head_only: bool = False,
        download_name: str | None = None,
    ) -> None:
        size = path.stat().st_size
        start = 0
        end = size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            unit, _, spec = range_header.partition("=")
            if unit == "bytes":
                left, _, right = spec.partition("-")
                try:
                    if left:
                        start = int(left)
                    if right:
                        end = int(right)
                    if not left and right:
                        suffix_length = int(right)
                        start = max(size - suffix_length, 0)
                        end = size - 1
                    end = min(end, size - 1)
                except ValueError:
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                if start < 0 or start >= size or end < start:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=3600")
        if download_name:
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{quote(download_name)}",
            )
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return

        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="H200 recording web UI")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--host")
    parser.add_argument("--user")
    parser.add_argument(
        "--config",
        default=os.environ.get("TAPO_CONFIG", str(h200.DEFAULT_CONFIG_PATH)),
    )
    parser.add_argument(
        "--key-file",
        default=os.environ.get("TAPO_KEY_FILE", str(h200.DEFAULT_KEY_PATH)),
    )
    return parser.parse_args()


def main() -> int:
    global STATE
    args = parse_args()
    STATE = AppState(
        config_path=args.config,
        key_file=args.key_file,
        host=args.host,
        user=args.user,
    )
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"H200 web UI listening on http://{args.bind}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
