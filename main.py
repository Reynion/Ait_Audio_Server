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
import torch
import yt_dlp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jobs import JobStatus, jobs
from supabase_client import (
    BUCKET,
    PITCH_SPEED_BUCKET,
    UPLOAD_BUCKET,
    YOUTUBE_BUCKET,
    cleanup_old_objects,
    delete_upload,
    download_stem,
    get_stem_public_url,
    upload_mix,
    upload_pitch_speed_audio,
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
VALID_STEMS = set(STEMS)
MIX_VOLUME_RANGE = (0.0, 2.0)
DEMUCS_MODEL = "htdemucs_ft"
DEMUCS_MODEL_PASSES = 4  # htdemucs_ft는 stem별 전담 모델 4개짜리 앙상블이라 진행률 바가 4번 반복됨. DEMUCS_MODEL 바꾸면 같이 바꿀 것.
PROGRESS_PATTERN = re.compile(r"(\d{1,3})%\|")
API_KEY = os.getenv("API_KEY")

# CPU 빌드 torch가 깔린 venv(demucs-env, start-server-cpu.bat)로 띄우면 자동으로 cpu,
# CUDA 빌드 torch가 깔린 venv(demucs-env-gpu, start-server-gpu.bat)로 띄우면 자동으로 cuda가 잡힌다.
# 요청/응답 형식은 device와 무관하게 완전히 동일 — 어느 배치파일로 켰는지가 유일한 결정 요인.
DEMUCS_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
# GPU는 청크를 여러 개 동시에 밀어넣으면(-j를 CPU처럼 높게 잡으면) 카드에 따라 VRAM이 부족할 수 있어
# 보수적으로 1로 잡는다. 필요하면 .env의 DEMUCS_GPU_JOBS로 조절 가능.
DEMUCS_JOBS = os.getenv("DEMUCS_GPU_JOBS", "1") if DEMUCS_DEVICE == "cuda" else "16"

_device_info = f" ({torch.cuda.get_device_name(0)})" if DEMUCS_DEVICE == "cuda" else ""
print(f"[startup] Demucs device: {DEMUCS_DEVICE}{_device_info}", flush=True)

# yt-dlp가 1800개+ 사이트를 지원하기 때문에 도메인 검증 없이 URL을 넘기면 범용 다운로드
# 프록시로 악용될 수 있어, 유튜브 도메인만 허용한다.
YOUTUBE_HOSTS_SUFFIX = ("youtube.com", "youtu.be")
YOUTUBE_MAX_DURATION_SECONDS = 30 * 60

# 프론트 슬라이더 범위(속도 0.5~2.0배, 피치 ±12반음)와 맞춤 — 서버에서도 같은 범위로 검증한다.
PITCH_SPEED_TEMPO_RANGE = (0.5, 2.0)
PITCH_SPEED_PITCH_RANGE = (-12, 12)

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
PITCH_SPEED_RETENTION_HOURS = 0.25

app = FastAPI(title="ait_audio_server")


def cleanup_loop() -> None:
    while True:
        try:
            n_uploads = cleanup_old_objects(UPLOAD_BUCKET, UPLOAD_RETENTION_HOURS / 24)
            n_results = cleanup_old_objects(BUCKET, RESULT_RETENTION_HOURS / 24)
            n_youtube = cleanup_old_objects(YOUTUBE_BUCKET, YOUTUBE_RETENTION_HOURS / 24)
            n_pitch_speed = cleanup_old_objects(PITCH_SPEED_BUCKET, PITCH_SPEED_RETENTION_HOURS / 24)
            print(
                f"[cleanup] {UPLOAD_BUCKET} {n_uploads}개, {BUCKET} {n_results}개, "
                f"{YOUTUBE_BUCKET} {n_youtube}개, {PITCH_SPEED_BUCKET} {n_pitch_speed}개 삭제",
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


class MixItem(BaseModel):
    key: str
    stems: list[str]
    volumes: dict[str, float] | None = None


class MixRequest(BaseModel):
    job_id: str
    mixes: list[MixItem]


class PitchSpeedRequest(BaseModel):
    file_url: str
    tempo: float = 1.0
    pitch: float = 0.0


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "demucs_device": DEMUCS_DEVICE}


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


@app.post("/mix")
def mix(
    body: MixRequest,
    x_api_key: str | None = Header(default=None),
) -> dict:
    """분리 결과가 이미 나온 곡에서, 원하는 파트 조합을 그때그때 여러 개 골라서 섞어 받는다.
    재분리 없이 이미 업로드된 stem mp3들을 다운로드/믹싱만 하므로 비용이 낮다."""
    verify_api_key(x_api_key)

    if not body.mixes:
        raise HTTPException(status_code=400, detail="mixes가 비어있습니다.")

    seen_keys: set[str] = set()
    for item in body.mixes:
        if not item.key:
            raise HTTPException(status_code=400, detail="key가 비어있는 항목이 있습니다.")
        if item.key in seen_keys:
            raise HTTPException(status_code=400, detail=f"key가 중복되었습니다: {item.key}")
        seen_keys.add(item.key)

        if not item.stems:
            raise HTTPException(status_code=400, detail=f"{item.key}: stems가 비어있습니다.")
        if len(set(item.stems)) != len(item.stems):
            raise HTTPException(status_code=400, detail=f"{item.key}: stems 안에 중복된 값이 있습니다.")
        unknown = set(item.stems) - VALID_STEMS
        if unknown:
            raise HTTPException(status_code=400, detail=f"{item.key}: 알 수 없는 stem입니다: {sorted(unknown)}")

        if item.volumes:
            unknown_volume_stems = set(item.volumes) - set(item.stems)
            if unknown_volume_stems:
                raise HTTPException(
                    status_code=400,
                    detail=f"{item.key}: volumes에 stems에 없는 값이 있습니다: {sorted(unknown_volume_stems)}",
                )
            for stem, vol in item.volumes.items():
                if not (MIX_VOLUME_RANGE[0] <= vol <= MIX_VOLUME_RANGE[1]):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{item.key}: {stem}의 volume은 {MIX_VOLUME_RANGE[0]}~{MIX_VOLUME_RANGE[1]} 범위여야 합니다.",
                    )

    job_id2 = uuid.uuid4().hex
    jobs.create(job_id2, filename=f"mix of {body.job_id}")
    executor.submit(process_mix_job, job_id2, body.job_id, body.mixes)

    return {"job_id": job_id2, "status": JobStatus.QUEUED.value}


@app.post("/pitch-speed")
async def pitch_speed(
    body: PitchSpeedRequest,
    x_api_key: str | None = Header(default=None),
) -> dict:
    """rubberband(ffmpeg 필터)로 피치/속도를 조절한다. 프론트의 실시간 미리듣기(SoundTouch.js, 클라이언트)는
    그대로 두고, 최종 내보내기만 이 엔드포인트로 돌려서 포먼트 보존 등 더 나은 품질을 준다."""
    verify_api_key(x_api_key)

    if not (PITCH_SPEED_TEMPO_RANGE[0] <= body.tempo <= PITCH_SPEED_TEMPO_RANGE[1]):
        raise HTTPException(
            status_code=400,
            detail=f"tempo는 {PITCH_SPEED_TEMPO_RANGE[0]}~{PITCH_SPEED_TEMPO_RANGE[1]} 범위여야 합니다.",
        )
    if not (PITCH_SPEED_PITCH_RANGE[0] <= body.pitch <= PITCH_SPEED_PITCH_RANGE[1]):
        raise HTTPException(
            status_code=400,
            detail=f"pitch는 {PITCH_SPEED_PITCH_RANGE[0]}~{PITCH_SPEED_PITCH_RANGE[1]} 범위여야 합니다.",
        )

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
        print(f"[pitch-speed] stem-uploads 원본 삭제 실패(무시하고 계속 진행): {exc}", flush=True)

    jobs.create(job_id, filename=url_path.name or input_path.name)
    executor.submit(process_pitch_speed_job, job_id, input_path, body.tempo, body.pitch)

    return {"job_id": job_id, "status": JobStatus.QUEUED.value}


INSTRUMENTAL_SOURCE_STEMS = ("drums", "bass", "other")


def mix_stems_ffmpeg(inputs: list[Path], volumes: list[float], output_path: Path) -> bool:
    """여러 mp3 stem을 재분리 없이 stem별 볼륨(1.0=원곡)을 적용해서 ffmpeg로 하나로 섞는다.
    입력이 1개면 amix 없이 volume 필터만 적용한다(단일 stem 볼륨 조절용)."""
    cmd = ["ffmpeg", "-y"]
    for p in inputs:
        cmd += ["-i", str(p)]

    if len(inputs) == 1:
        cmd += ["-af", f"volume={volumes[0]}"]
    else:
        parts = [f"[{i}:a]volume={vol}[a{i}]" for i, vol in enumerate(volumes)]
        labels = "".join(f"[a{i}]" for i in range(len(inputs)))
        parts.append(f"{labels}amix=inputs={len(inputs)}:duration=longest:normalize=0")
        cmd += ["-filter_complex", ";".join(parts)]

    cmd += ["-b:a", "320k", str(output_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not output_path.exists():
        print(f"[mix] ffmpeg 믹싱 실패: {result.stderr[-500:]}", flush=True)
        return False
    return True


def build_instrumental(stem_dir: Path) -> Path | None:
    """보컬을 뺀 나머지 stem들을 합쳐 반주(instrumental) 트랙을 만든다."""
    inputs = [stem_dir / f"{s}.mp3" for s in INSTRUMENTAL_SOURCE_STEMS if (stem_dir / f"{s}.mp3").exists()]
    if len(inputs) < 2:
        return None

    output_path = stem_dir / "instrumental.mp3"
    if not mix_stems_ffmpeg(inputs, [1.0] * len(inputs), output_path):
        return None
    return output_path


def run_demucs(job_id: str, input_path: Path, job_output_dir: Path) -> None:
    """Demucs를 서브프로세스로 돌리면서 tqdm 진행률(%) 출력을 실시간으로 읽어 job.progress에 반영한다."""
    cmd = [
        sys.executable, "-m", "demucs",
        "-n", DEMUCS_MODEL,
        "--device", DEMUCS_DEVICE,
        "-j", DEMUCS_JOBS,
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


def process_mix_job(job_id2: str, source_job_id: str, mix_items: list[MixItem]) -> None:
    work_dir = OUTPUT_DIR / job_id2
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        jobs.update(job_id2, status=JobStatus.PROCESSING, progress=0)

        # 조합들이 요구하는 stem을 합쳐서 필요한 것만 한 번씩 받는다(같은 stem을 여러 조합이
        # 같이 써도 중복 다운로드하지 않기 위함). 존재하지 않으면(원본이 1시간 지나 만료됨 등)
        # 그 stem이 빠진 채로 넘어가고, 그 stem이 필요한 조합만 나중에 스킵된다.
        needed_stems = sorted({s for item in mix_items for s in item.stems})
        stem_paths: dict[str, Path] = {}
        for stem in needed_stems:
            try:
                data = download_stem(source_job_id, stem)
            except Exception as exc:  # noqa: BLE001
                print(f"[mix] {source_job_id}/{stem} 다운로드 실패(만료되었을 수 있음): {exc}", flush=True)
                continue
            stem_path = work_dir / f"{stem}.mp3"
            stem_path.write_bytes(data)
            stem_paths[stem] = stem_path

        jobs.update(job_id2, status=JobStatus.UPLOADING, progress=30)

        urls: dict[str, str] = {}
        total = len(mix_items)
        for i, item in enumerate(mix_items):
            available = [s for s in item.stems if s in stem_paths]
            if len(available) != len(item.stems):
                continue  # 필요한 stem 중 일부가 만료/누락 -> 이 조합은 스킵

            volumes = [(item.volumes or {}).get(s, 1.0) for s in available]
            if len(available) == 1 and volumes[0] == 1.0:
                # 볼륨 조절이 없는 단일 stem은 믹싱 없이 원본 URL을 그대로 재사용(비용 0)
                urls[item.key] = get_stem_public_url(source_job_id, available[0])
            else:
                output_path = work_dir / f"mix_{item.key}.mp3"
                if mix_stems_ffmpeg([stem_paths[s] for s in available], volumes, output_path):
                    urls[item.key] = upload_mix(source_job_id, item.key, output_path)

            jobs.update(job_id2, progress=min(99, 30 + int((i + 1) / total * 69)))

        if not urls:
            raise RuntimeError("생성된 조합이 없습니다(원본 stem이 만료되었거나 job_id가 잘못됐을 수 있습니다).")

        jobs.update(job_id2, status=JobStatus.COMPLETED, progress=100, urls=urls)
    except Exception as exc:  # noqa: BLE001
        jobs.update(job_id2, status=JobStatus.FAILED, error=str(exc))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def run_pitch_speed_ffmpeg(input_path: Path, output_path: Path, tempo: float, pitch_semitones: float) -> None:
    """ffmpeg rubberband 필터로 피치/속도를 조절한다. pitch는 반음 단위로 받아서
    rubberband가 요구하는 배율(scale factor)로 환산한다(2^(반음/12))."""
    pitch_scale = 2 ** (pitch_semitones / 12)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-af", f"rubberband=tempo={tempo}:pitch={pitch_scale}:formant=preserved",
        "-b:a", "320k",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not output_path.exists():
        raise RuntimeError(f"피치/속도 조절 실패: {result.stderr[-2000:]}")


def process_pitch_speed_job(job_id: str, input_path: Path, tempo: float, pitch: float) -> None:
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        jobs.update(job_id, status=JobStatus.PROCESSING, progress=20)
        output_path = job_dir / "output.mp3"
        run_pitch_speed_ffmpeg(input_path, output_path, tempo, pitch)
        jobs.update(job_id, progress=80)

        jobs.update(job_id, status=JobStatus.UPLOADING)
        audio_url = upload_pitch_speed_audio(job_id, output_path)

        jobs.update(job_id, status=JobStatus.COMPLETED, progress=100, urls={"audio": audio_url})
    except Exception as exc:  # noqa: BLE001
        jobs.update(job_id, status=JobStatus.FAILED, error=str(exc))
    finally:
        shutil.rmtree(input_path.parent, ignore_errors=True)
        shutil.rmtree(job_dir, ignore_errors=True)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": str(exc)})
