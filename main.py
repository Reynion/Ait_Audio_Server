from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jobs import JobStatus, jobs
from supabase_client import (
    BUCKET,
    UPLOAD_BUCKET,
    YOUTUBE_BUCKET,
    cleanup_old_objects,
    delete_upload,
    upload_stem,
    upload_youtube_audio,
)

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# winget으로 설치한 FFmpeg는 PATH가 갱신되려면 셸/로그인을 다시 해야 하므로,
# 재부팅 없이도 바로 동작하도록 이 프로세스의 PATH에 직접 추가한다.
FFMPEG_DIR = os.getenv("FFMPEG_DIR")
if FFMPEG_DIR:
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

ALLOWED_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a"}
STEMS = ("vocals", "drums", "bass", "other")
DEMUCS_MODEL = "htdemucs_ft"
DEMUCS_MODEL_PASSES = 4  # htdemucs_ft는 stem별 전담 모델 4개짜리 앙상블이라 진행률 바가 4번 반복됨. DEMUCS_MODEL 바꾸면 같이 바꿀 것.
PROGRESS_PATTERN = re.compile(r"(\d{1,3})%\|")
API_KEY = os.getenv("API_KEY")

# yt-dlp가 1800개+ 사이트를 지원하기 때문에 도메인 검증 없이 URL을 넘기면 범용 다운로드
# 프록시로 악용될 수 있어, 유튜브 도메인만 허용한다.
YOUTUBE_HOSTS_SUFFIX = ("youtube.com", "youtu.be")
YOUTUBE_MAX_DURATION_SECONDS = 30 * 60

# CPU 한 대에서 Demucs(-j 16)를 동시에 여러 개 돌리면 코어를 나눠 쓰게 되어
# 오히려 전체 처리 시간이 늘어나므로, 워커 1개로 작업을 순차 처리한다.
executor = ThreadPoolExecutor(max_workers=1)

# stem-uploads(원본)는 다운로드 즉시 지우지만, 혹시 놓친 게 있을 때를 대비한 안전망 겸
# separated-audio(결과)는 사용자가 다운로드할 시간을 준 뒤 일정 기간 지나면 자동 정리한다.
# 결과는 다시 뽑으면 그만이라 보관 기간을 짧게 잡고, 그만큼 정리 주기도 촘촘하게 돈다.
CLEANUP_INTERVAL_SECONDS = 15 * 60
UPLOAD_RETENTION_HOURS = 24
RESULT_RETENTION_HOURS = 1
YOUTUBE_RETENTION_HOURS = 0.25

app = FastAPI(title="ait_audio_server")


def cleanup_loop() -> None:
    while True:
        try:
            n_uploads = cleanup_old_objects(UPLOAD_BUCKET, UPLOAD_RETENTION_HOURS / 24)
            n_results = cleanup_old_objects(BUCKET, RESULT_RETENTION_HOURS / 24)
            n_youtube = cleanup_old_objects(YOUTUBE_BUCKET, YOUTUBE_RETENTION_HOURS / 24)
            print(
                f"[cleanup] {UPLOAD_BUCKET} {n_uploads}개, {BUCKET} {n_results}개, "
                f"{YOUTUBE_BUCKET} {n_youtube}개 삭제",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[cleanup] 실패: {exc}", flush=True)
        time.sleep(CLEANUP_INTERVAL_SECONDS)


@app.on_event("startup")
def start_cleanup_thread() -> None:
    threading.Thread(target=cleanup_loop, daemon=True).start()


def verify_api_key(x_api_key: str | None) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="유효하지 않은 API 키입니다.")


def is_allowed_youtube_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in YOUTUBE_HOSTS_SUFFIX or any(host.endswith(f".{suffix}") for suffix in YOUTUBE_HOSTS_SUFFIX)


class SeparateRequest(BaseModel):
    file_url: str


class YoutubeAudioRequest(BaseModel):
    url: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/separate")
async def separate(
    body: SeparateRequest,
    x_api_key: str | None = Header(default=None),
) -> dict:
    verify_api_key(x_api_key)

    url_path = Path(urlparse(body.file_url).path)
    ext = url_path.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="mp3, wav, flac, ogg, m4a 파일만 지원합니다.")

    job_id = uuid.uuid4().hex
    job_upload_dir = UPLOAD_DIR / job_id
    job_upload_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_upload_dir / f"input{ext}"

    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(body.file_url)
            resp.raise_for_status()
            input_path.write_bytes(resp.content)
    except httpx.HTTPError as exc:
        shutil.rmtree(job_upload_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"파일 다운로드 실패: {exc}")

    try:
        delete_upload(body.file_url)
    except Exception as exc:  # noqa: BLE001
        print(f"[separate] stem-uploads 원본 삭제 실패(무시하고 계속 진행): {exc}", flush=True)

    jobs.create(job_id, filename=url_path.name or input_path.name)
    executor.submit(process_job, job_id, input_path)

    return {"job_id": job_id, "status": JobStatus.QUEUED.value}


@app.get("/status/{job_id}")
def status(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="존재하지 않는 job_id 입니다.")
    return job.to_dict()


@app.post("/youtube-audio")
def youtube_audio(
    body: YoutubeAudioRequest,
    x_api_key: str | None = Header(default=None),
) -> dict:
    """비동기 def가 아닌 이유: yt_dlp 호출이 블로킹이라, FastAPI가 자동으로 돌려주는
    스레드풀에서 실행되도록 동기 함수로 둔다(이벤트 루프를 막지 않기 위함)."""
    verify_api_key(x_api_key)
    if not is_allowed_youtube_host(body.url):
        raise HTTPException(status_code=400, detail="youtube.com 또는 youtu.be 링크만 지원합니다.")

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}) as ydl:
        try:
            info = ydl.extract_info(body.url, download=False)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"영상 정보를 가져오지 못했습니다: {exc}")

    duration = info.get("duration") or 0
    if duration > YOUTUBE_MAX_DURATION_SECONDS:
        raise HTTPException(status_code=400, detail="30분을 초과하는 영상은 지원하지 않습니다.")

    job_id = uuid.uuid4().hex
    jobs.create(job_id, filename=info.get("title") or job_id)
    executor.submit(process_youtube_job, job_id, body.url)

    return {"job_id": job_id, "status": JobStatus.QUEUED.value}


INSTRUMENTAL_SOURCE_STEMS = ("drums", "bass", "other")


def build_instrumental(stem_dir: Path) -> Path | None:
    """보컬을 뺀 나머지 stem들을 합쳐 반주(instrumental) 트랙을 만든다."""
    inputs = [stem_dir / f"{s}.mp3" for s in INSTRUMENTAL_SOURCE_STEMS if (stem_dir / f"{s}.mp3").exists()]
    if len(inputs) < 2:
        return None

    output_path = stem_dir / "instrumental.mp3"
    cmd = ["ffmpeg", "-y"]
    for p in inputs:
        cmd += ["-i", str(p)]
    cmd += [
        "-filter_complex", f"amix=inputs={len(inputs)}:duration=longest:normalize=0",
        "-b:a", "320k",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not output_path.exists():
        print(f"[separate] 반주 트랙 생성 실패(무시하고 계속 진행): {result.stderr[-500:]}", flush=True)
        return None
    return output_path


def run_demucs(job_id: str, input_path: Path, job_output_dir: Path) -> None:
    """Demucs를 서브프로세스로 돌리면서 tqdm 진행률(%) 출력을 실시간으로 읽어 job.progress에 반영한다."""
    cmd = [
        sys.executable, "-m", "demucs",
        "-n", DEMUCS_MODEL,
        "--device", "cpu",
        "-j", "16",
        "--overlap", "0.5",
        "--mp3", "--mp3-bitrate", "320",
        "-o", str(job_output_dir),
        str(input_path),
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    output_lines: list[str] = []
    pass_index = 0
    last_percent = 0
    for line in proc.stdout:
        output_lines.append(line)
        match = PROGRESS_PATTERN.search(line)
        if not match:
            continue
        percent = int(match.group(1))
        if percent < last_percent - 20:
            # 퍼센트가 갑자기 뚝 떨어짐 = 다음 stem 전담 모델(pass)로 넘어간 것
            pass_index = min(pass_index + 1, DEMUCS_MODEL_PASSES - 1)
        last_percent = percent
        overall = min(99, (pass_index * 100 + percent) // DEMUCS_MODEL_PASSES)
        jobs.update(job_id, progress=overall)

    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"Demucs 처리 실패: {''.join(output_lines)[-2000:]}")
    jobs.update(job_id, progress=100)


def process_job(job_id: str, input_path: Path) -> None:
    job_output_dir = OUTPUT_DIR / job_id
    try:
        jobs.update(job_id, status=JobStatus.PROCESSING, progress=0)
        run_demucs(job_id, input_path, job_output_dir)

        stem_dir = job_output_dir / DEMUCS_MODEL / input_path.stem

        jobs.update(job_id, status=JobStatus.UPLOADING)
        urls: dict[str, str] = {}
        for stem in STEMS:
            stem_file = stem_dir / f"{stem}.mp3"
            if stem_file.exists():
                urls[stem] = upload_stem(job_id, stem, stem_file)

        try:
            instrumental_path = build_instrumental(stem_dir)
            if instrumental_path:
                urls["instrumental"] = upload_stem(job_id, "instrumental", instrumental_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[separate] 반주 트랙 처리 실패(무시하고 계속 진행): {exc}", flush=True)

        if not urls:
            raise RuntimeError("분리된 결과 파일을 찾을 수 없습니다.")

        jobs.update(job_id, status=JobStatus.COMPLETED, urls=urls)
    except Exception as exc:  # noqa: BLE001
        jobs.update(job_id, status=JobStatus.FAILED, error=str(exc))
    finally:
        shutil.rmtree(input_path.parent, ignore_errors=True)
        shutil.rmtree(job_output_dir, ignore_errors=True)


def make_youtube_progress_hook(job_id: str):
    def hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)
        if total:
            # 다운로드 완료 후 mp3 변환(ffmpeg 후처리)이 남아있으므로 100%는 아껴둔다.
            jobs.update(job_id, progress=min(99, int(downloaded / total * 99)))
    return hook


def process_youtube_job(job_id: str, url: str) -> None:
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        jobs.update(job_id, status=JobStatus.PROCESSING, progress=0)

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestaudio/best",
            "outtmpl": str(job_dir / "audio.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }],
            "progress_hooks": [make_youtube_progress_hook(job_id)],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        jobs.update(job_id, progress=100)

        output_path = job_dir / "audio.mp3"
        if not output_path.exists():
            raise RuntimeError("오디오 추출 결과 파일을 찾을 수 없습니다.")

        jobs.update(job_id, status=JobStatus.UPLOADING)
        audio_url = upload_youtube_audio(job_id, output_path)

        jobs.update(job_id, status=JobStatus.COMPLETED, urls={"audio": audio_url})
    except Exception as exc:  # noqa: BLE001
        jobs.update(job_id, status=JobStatus.FAILED, error=str(exc))
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": str(exc)})
