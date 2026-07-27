# Demucs 음원 분리 로컬 서버

AIT 프로젝트(Next.js)에서 사용할 음원 분리(Demucs) FastAPI 서버. Windows 로컬 PC(CPU 전용)에서
실행하고, Cloudflare Tunnel로 외부(Vercel에 배포된 Next.js)에서 접근할 수 있게 한다.

## 1. 가상환경 생성 및 패키지 설치

PowerShell 기준:

```powershell
cd E:\MusicSeparator
python -m venv demucs-env
.\demucs-env\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

> Demucs가 의존하는 PyTorch는 용량이 크고 설치에 시간이 걸린다(첫 설치 시 수 분~수십 분 소요 가능).
> `requirements.txt`에 `torch`/`torchaudio`를 `2.4.1`로 고정해둔 상태다. 최신 torchaudio(2.9+)는 오디오
> 로딩 백엔드가 `torchcodec` 전용으로 바뀌어 demucs 4.0.1과 호환되지 않으므로 임의로 버전을 올리지 말 것.
> 시스템에 FFmpeg가 없어도 `soundfile` 패키지가 mp3/wav/flac/ogg 디코딩을 대신 처리한다.
> 단, m4a(AAC)는 soundfile이 못 읽으므로 FFmpeg가 필요하다 — `winget install --id Gyan.FFmpeg`로 설치 후
> `.env`의 `FFMPEG_DIR`에 설치된 `bin` 폴더 경로를 지정한다(설치 직후엔 PATH가 새 셸에만 반영되므로,
> `FFMPEG_DIR`을 지정해두면 재부팅/재로그인 없이도 바로 동작함).

## 2. 환경변수 설정

`.env.example`을 복사해서 `.env` 생성 후 값 채우기:

```powershell
copy .env.example .env
```

- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`: Supabase 프로젝트 설정 > API에서 확인
- `SUPABASE_BUCKET`: 기본값 `separated-audio` — 분리 결과(vocals/drums/bass/other) 저장용, public
- `SUPABASE_UPLOAD_BUCKET`: 기본값 `stem-uploads` — Next.js가 올려주는 원본 파일을 받는 용도, public,
  50MB 제한(Supabase 프로젝트 플랜의 전역 업로드 한도 때문에 이 이상은 프로젝트 설정을 먼저 올려야 함)
- `API_KEY`: Cloudflare Tunnel로 외부에 노출되는 서버이므로, 임의의 값을 넣어 인증 없는 요청을 막는 것을 권장.
  설정하면 Next.js 쪽에서 모든 요청에 `X-API-Key` 헤더를 함께 보내야 함.

## 3. 서버 + Cloudflare Tunnel 실행

`run.py` 하나로 uvicorn 서버와 Cloudflare Quick Tunnel을 동시에 띄운다. 터널이 뜨면서 발급되는
`https://xxxx.trycloudflare.com` URL을 자동으로 Supabase `demucs_server` 테이블(`id=1`)에 기록하므로,
Next.js 쪽은 매 요청마다 그 테이블에서 최신 URL을 읽어간다 — 재시작해서 URL이 바뀌어도 손댈 곳이 없다.

```powershell
.\demucs-env\Scripts\Activate.ps1
python run.py
```

매번 이렇게 치기 번거로우면 `start-server.bat`을 더블클릭하면 새 창에서 켜지고, `stop-server.bat`을
더블클릭하면 꺼진다(또는 그냥 서버 창을 직접 닫아도 됨).

- `GET /health` — 서버 상태 확인
- `POST /separate` — JSON `{ "file_url": "https://.../stem-uploads/..." }`, 헤더 `X-API-Key: <API_KEY>` 필요
  → `{"job_id": "...", "status": "queued"}` 반환. 파일 자체를 요청 본문으로 안 받고 URL만 받아서 서버가 직접
  다운로드한다 — Next.js API Route(Vercel Functions)는 요청 본문이 4.5MB로 제한돼 있어서 곡 파일을 그대로
  중계할 수 없기 때문. 다운로드 성공 직후 `stem-uploads`의 원본은 바로 지운다.
- `GET /status/{job_id}` — 처리 상태 조회. `status`는 `queued → processing → uploading → completed`(또는 `failed`) 순서로 바뀌며,
  `completed` 시 `urls`에 `vocals`/`drums`/`bass`/`other`/`instrumental` Supabase Storage 공개 URL(mp3, 320kbps)이 담김.
  `instrumental`은 drums+bass+other를 ffmpeg `amix`로 합친 보컬 제외(반주) 트랙 — 재분리 없이 이미 나온 stem을
  섞기만 하는 거라 추가 비용이 거의 없음. ffmpeg 실패 시에도 나머지 4개 stem은 정상 반환되고 `instrumental`만 빠짐.
  `progress`(0~100 정수)는 `status`가 `processing`일 때 Demucs가 찍는 tqdm 진행률을 실시간으로 파싱해서 채워줌
  — `htdemucs_ft`는 stem 전담 모델 4개가 순서대로 도는 구조라(`DEMUCS_MODEL_PASSES`), 그 4단계를 하나의 0~100
  값으로 환산함. `uploading`/`completed`로 넘어가면 100.

동시에 여러 곡을 요청해도 서버 내부에서 큐로 순차 처리한다(CPU 코어를 Demucs `-j 16`이 이미 최대로 쓰기 때문에
동시 처리 시 오히려 전체 시간이 늘어남).

cloudflared 실행파일이 없다면 아래로 다시 받는다 (`cloudflared/` 폴더는 용량 때문에 git에 커밋하지 않음):

```powershell
mkdir cloudflared
curl -L -o cloudflared\cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
```

`demucs_server` 테이블이 없다면 Supabase 대시보드 SQL Editor에서 한 번 생성해야 한다:

```sql
create table if not exists public.demucs_server (
  id smallint primary key default 1,
  url text not null default '',
  updated_at timestamptz not null default now(),
  constraint demucs_server_singleton check (id = 1)
);
insert into public.demucs_server (id, url) values (1, '') on conflict (id) do nothing;
alter table public.demucs_server enable row level security;
```

`stem-uploads` 버킷(원본 업로드용)은 로그인한 사용자만 올릴 수 있게 RLS를 걸어둔다:

```sql
create policy "authenticated users can upload to stem-uploads"
on storage.objects for insert
to authenticated
with check (bucket_id = 'stem-uploads');

create policy "authenticated users can overwrite their stem-uploads"
on storage.objects for update
to authenticated
using (bucket_id = 'stem-uploads');
```

## 참고

- 모델은 `htdemucs_ft`(4-모델 앙상블 파인튜닝, 품질 우선) 사용 중. `main.py`의 `DEMUCS_MODEL`을 `htdemucs`로
  바꾸면 품질은 약간 떨어지지만 약 4배 빠름.
- `--overlap 0.5`(기본 0.25)로 구간 경계 이음새 아티팩트를 줄임. 처리해야 할 구간 수가 늘어나 조금 느려지지만
  `--shifts`처럼 배수로 곱해지는 게 아니라 완만하게(대략 1.5배) 늘어나는 정도라 비용 대비 효과가 좋음.
- 3~5분짜리 곡 기준 CPU 처리 시간 약 3~5분(`htdemucs` 기준. `htdemucs_ft`는 그보다 오래 걸림)
- 처리 완료/실패 후 로컬 업로드 파일과 Demucs 산출물은 자동 삭제됨(Supabase Storage에만 보관)
- Supabase Storage 정리: `stem-uploads`(원본)는 다운로드 직후 즉시 삭제, `separated-audio`(결과)는
  업로드 후 1시간 지나면 자동 삭제됨(다시 뽑는 비용이 크지 않아 짧게 잡음). 서버 시작 시 한 번 +
  이후 15분마다 정리 스레드가 돎. 보관 기간은 `main.py`의 `UPLOAD_RETENTION_HOURS`/`RESULT_RETENTION_HOURS`로,
  정리 주기는 `CLEANUP_INTERVAL_SECONDS`로 조절 가능.
