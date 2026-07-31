from __future__ import annotations

import datetime
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlparse

from supabase import Client, create_client

BUCKET = os.getenv("SUPABASE_BUCKET", "separated-audio")
UPLOAD_BUCKET = os.getenv("SUPABASE_UPLOAD_BUCKET", "stem-uploads")
YOUTUBE_BUCKET = os.getenv("SUPABASE_YOUTUBE_BUCKET", "youtube-audio")


@lru_cache
def get_supabase() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY 환경변수가 설정되지 않았습니다.")
    return create_client(url, key)


def update_tunnel_url(url: str) -> None:
    client = get_supabase()
    client.table("demucs_server").upsert({
        "id": 1,
        "url": url,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }).execute()


def upload_stem(job_id: str, stem_name: str, file_path: Path) -> str:
    client = get_supabase()
    storage_path = f"{job_id}/{stem_name}{file_path.suffix}"
    content_type = "audio/mpeg" if file_path.suffix == ".mp3" else "audio/wav"
    data = file_path.read_bytes()
    client.storage.from_(BUCKET).upload(
        storage_path,
        data,
        {"content-type": content_type, "upsert": "true"},
    )
    return client.storage.from_(BUCKET).get_public_url(storage_path)


def upload_youtube_audio(job_id: str, file_path: Path) -> str:
    client = get_supabase()
    storage_path = f"{job_id}/audio{file_path.suffix}"
    data = file_path.read_bytes()
    client.storage.from_(YOUTUBE_BUCKET).upload(
        storage_path,
        data,
        {"content-type": "audio/mpeg", "upsert": "true"},
    )
    return client.storage.from_(YOUTUBE_BUCKET).get_public_url(storage_path)


def _extract_storage_path(bucket: str, url: str) -> str | None:
    marker = f"/object/public/{bucket}/"
    path = urlparse(url).path
    if marker not in path:
        return None
    return unquote(path.split(marker, 1)[1])


def delete_upload(file_url: str) -> None:
    """분리 요청 시 받은 원본 업로드 파일을, 로컬로 다운로드한 뒤 바로 지운다."""
    storage_path = _extract_storage_path(UPLOAD_BUCKET, file_url)
    if not storage_path:
        return
    client = get_supabase()
    client.storage.from_(UPLOAD_BUCKET).remove([storage_path])


def cleanup_old_objects(bucket: str, max_age_days: float) -> int:
    """bucket 안의 {job_id}/{file} 구조 오브젝트 중 max_age_days보다 오래된 걸 지운다."""
    client = get_supabase()
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=max_age_days)

    deleted = 0
    for folder in client.storage.from_(bucket).list():
        job_id = folder.get("name")
        if not job_id:
            continue
        to_delete = []
        for entry in client.storage.from_(bucket).list(path=job_id):
            created_at_str = entry.get("created_at")
            if not created_at_str:
                continue
            created_at = datetime.datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            if created_at < cutoff:
                to_delete.append(f"{job_id}/{entry['name']}")
        if to_delete:
            client.storage.from_(bucket).remove(to_delete)
            deleted += len(to_delete)
    return deleted
