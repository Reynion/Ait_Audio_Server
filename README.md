# ait_audio_server

ait_projekt(Next.js)에서 쓰는 오디오 처리 로컬 서버. 음원 분리(Demucs)와 유튜브 오디오 추출을
담당한다. Windows 로컬 PC에서 실행하고, Cloudflare Tunnel로 외부(Vercel에 배포된 Next.js)에서
접근할 수 있게 한다. (예전 이름: MusicSeparator — 음원 분리 외 기능이 추가되면서 개명함)

## 1. 가상환경 생성 및 패키지 설치

PowerShell 기준. **CPU용(`demucs-env`)과 GPU용(`demucs-env-gpu`) 가상환경을 따로 만든다** — 코드는
완전히 동일하고, 어느 가상환경으로 서버를 띄우느냐(아래 3번의 `start-server-cpu.bat`/`start-server-gpu.bat`)만
다르다.

```powershell
cd E:\ait_audio_server

# CPU용
python -m venv demucs-env
.\demucs-env\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
deactivate
```

호환되는 NVIDIA GPU(CUDA 지원, 최소 몇 년 이내 세대 — 오래된 카드는 PyTorch가 커널을 안 지원해서 그냥
CPU로 도는 것과 다를 바 없으니 의미 없음)가 있으면 GPU용도 추가로 만들 수 있다:

```powershell
# GPU용 (CUDA 빌드 torch를 먼저 설치한 뒤 requirements.txt를 설치해야 CPU 빌드로 덮어써지지 않는다)
python -m venv demucs-env-gpu
.\demucs-env-gpu\Scripts\Activate.ps1
pip install --upgrade pip
pip install torch==2.4.1 torchaudio==2.4.1 --index-url https://download.pytorch.org/whl/cu118
pip install -r requirements.txt
deactivate
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
- `SUPABASE_YOUTUBE_BUCKET`: 기본값 `youtube-audio` — 유튜브에서 추출한 오디오(mp3) 저장용, public.
  버킷이 없으면 Supabase 대시보드(Storage → New bucket, public으로 생성)에서 미리 만들어야 함.
- `SUPABASE_PITCH_SPEED_BUCKET`: 기본값 `pitch-speed-audio` — 피치/속도 조절 결과(mp3) 저장용, public.
  마찬가지로 버킷을 미리 만들어야 함.
- `API_KEY`: Cloudflare Tunnel로 외부에 노출되는 서버이므로, 임의의 값을 넣어 인증 없는 요청을 막는 것을 권장.
  설정하면 Next.js 쪽에서 모든 요청에 `X-API-Key` 헤더를 함께 보내야 함.

## 3. 서버 + Cloudflare Tunnel 실행

`run.py` 하나로 uvicorn 서버와 Cloudflare Quick Tunnel을 동시에 띄운다. 터널이 뜨면서 발급되는
`https://xxxx.trycloudflare.com` URL을 자동으로 Supabase `demucs_server` 테이블(`id=1`)에 기록하므로,
Next.js 쪽은 매 요청마다 그 테이블에서 최신 URL을 읽어간다 — 재시작해서 URL이 바뀌어도 손댈 곳이 없다.

```powershell
.\demucs-env\Scripts\Activate.ps1     # 또는 GPU용이면 .\demucs-env-gpu\Scripts\Activate.ps1
python run.py
```

매번 이렇게 치기 번거로우면 `start-server-cpu.bat`(또는 `start-server-gpu.bat`)을 더블클릭하면 새 창에서
켜지고, `stop-server.bat`을 더블클릭하면 꺼진다(또는 그냥 서버 창을 직접 닫아도 됨). 어느 쪽으로 띄웠는지는
`--device`가 자동으로 그 가상환경의 torch 빌드를 따라가므로(아래 "GPU 모드" 참고) API 요청/응답은 완전히
동일하다 — Next.js 쪽은 서버가 CPU로 떠있는지 GPU로 떠있는지 신경 쓸 필요 없음.

- `GET /health` — 서버 상태 확인. `demucs_device`로 현재 CPU/GPU 중 어느 걸로 떠있는지 참고용으로 확인 가능
  (요청 방식에는 영향 없음, 순수 디버깅/모니터링용).
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
- `POST /youtube-audio` — JSON `{ "url": "https://www.youtube.com/watch?v=..." }`, 헤더 `X-API-Key: <API_KEY>` 필요
  → `{"job_id": "...", "status": "queued"}` 반환. 응답 전에 영상 메타데이터만 먼저 조회해 검증한다:
  (1) `youtube.com`/`youtu.be` 도메인이 아니면 즉시 400(yt-dlp가 1800개+ 사이트를 지원하므로, 검증 없이 넘기면
  범용 다운로드 프록시로 악용될 수 있어 화이트리스트로 막음), (2) 영상 길이가 30분을 넘으면 즉시 400(실제
  다운로드 전에 걸러냄). 재생목록 링크를 넣어도 `noplaylist` 옵션으로 그 영상 하나만 처리한다. 처리는 `/status`를
  그대로 재사용(같은 job/status 시스템)하며, 완료 시 `urls`에 `audio` 하나만 담겨 온다(mp3, 320kbps).
- `POST /mix` — JSON `{ "job_id": "<이전 /separate 응답의 job_id>", "mixes": [{ "key": "...", "stems": [...] }] }`,
  헤더 `X-API-Key: <API_KEY>` 필요 → `{"job_id": "...", "status": "queued"}` 반환. 이미 분리된 stem들을
  원하는 조합으로 그때그때 섞어서 받는 엔드포인트(예: 드럼만 뺀 버전, 보컬+베이스만 있는 버전 등) —
  재분리 없이 이미 나온 mp3를 다운로드/`ffmpeg amix`로 섞기만 하므로 비용이 낮음.
  - `stems`는 `vocals`/`drums`/`bass`/`other` 중 1~4개 자유 조합. `mixes`에 여러 조합을 한 번에 담을 수 있고,
    그 조합들이 필요로 하는 stem은 합쳐서 딱 1번씩만 다운로드함(같은 stem을 여러 조합이 같이 써도 중복
    다운로드 안 함).
  - `key`는 요청한 값 그대로 응답에 돌아옴(백엔드는 의미를 해석하지 않고 결과 매핑용으로만 씀) — 같은 요청
    안에서 `key`가 중복되면 400.
  - `stems`가 1개뿐이면 믹싱 없이 원본 stem의 공개 URL을 그대로 돌려줌(비용 0).
  - 처리는 `/status`를 그대로 재사용하며, 완료 시 `urls`에 요청한 `key`별로 결과 URL이 담김. 조합 하나가
    실패해도(원본 stem 만료 등) 전체 job을 실패시키지 않고 해당 `key`만 `urls`에서 빠짐 — 요청한 모든 조합이
    실패했을 때만 job 전체가 `failed`.
  - **원본 stem은 `/separate` 완료 후 `RESULT_RETENTION_HOURS`(기본 1시간) 안에만 살아있음** — 그 이후 `/mix`를
    요청하면 관련 조합이 전부 빠지거나 실패함. Next.js 쪽에서 "다시 분리해주세요" 안내가 필요함.
- `POST /pitch-speed` — JSON `{ "file_url": "https://.../stem-uploads/...", "tempo": 1.0, "pitch": 0 }`,
  헤더 `X-API-Key: <API_KEY>` 필요 → `{"job_id": "...", "status": "queued"}` 반환. ffmpeg `rubberband` 필터로
  피치/속도를 조절함(`formant=preserved` 옵션으로 포먼트 보존 — 목소리 피치를 많이 바꿔도 "다람쥐"/"저음 괴물"처럼
  안 들리게 함). 프론트의 실시간 미리듣기(SoundTouch.js, 클라이언트에서 처리)는 그대로 두고, **최종
  내보내기(export)만** 이 엔드포인트를 거치는 용도로 설계함 — 슬라이더 조작마다 호출하면 안 되고, 값 확정 후
  한 번만 호출할 것.
  - `tempo`: 0.5~2.0 배율(1.0 = 원본 속도), `pitch`: -12~12 반음(0 = 원본 피치). 범위 벗어나면 400.
  - `/separate`와 동일하게 `file_url`을 다운로드해서 처리하고, `stem-uploads`에서 온 파일이면 다운로드 직후
    원본을 삭제함.
  - 처리는 `/status`를 그대로 재사용하며, 완료 시 `urls.audio`에 결과 URL 하나만 담김(mp3, 320kbps).

동시에 여러 곡/영상/믹스/피치조절을 요청해도 서버 내부에서 워커 1개짜리 큐로 순차 처리한다(CPU 코어를 Demucs
`-j 16`이 이미 최대로 쓰기 때문에 동시 처리 시 오히려 전체 시간이 늘어남 — 다른 기능들도 같은 큐를 공유함).

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

- **GPU 모드**: `main.py`가 시작할 때 `torch.cuda.is_available()`로 자동 감지해서 `DEMUCS_DEVICE`를
  `cpu`/`cuda`로, `-j`(병렬 작업 수)도 그에 맞게(CPU 16, GPU 1) 정한다. 즉 코드는 안 건드리고 어느
  가상환경(`demucs-env` vs `demucs-env-gpu`)으로 서버를 띄웠는지에 따라 자동으로 결정됨 — 1번 항목 참고.
  GPU 쪽 `-j`(기본 1)는 카드 VRAM에 따라 `.env`의 `DEMUCS_GPU_JOBS`로 조절 가능(값을 올리면 청크를 동시에
  여러 개 GPU에 밀어넣어 빨라질 수 있지만 VRAM 부족으로 실패할 수도 있음).
- 모델은 `htdemucs_ft`(4-모델 앙상블 파인튜닝, 품질 우선) 사용 중. `main.py`의 `DEMUCS_MODEL`을 `htdemucs`로
  바꾸면 품질은 약간 떨어지지만 약 4배 빠름.
- `--overlap 0.5`(기본 0.25)로 구간 경계 이음새 아티팩트를 줄임. 처리해야 할 구간 수가 늘어나 조금 느려지지만
  `--shifts`처럼 배수로 곱해지는 게 아니라 완만하게(대략 1.5배) 늘어나는 정도라 비용 대비 효과가 좋음.
- 3~5분짜리 곡 기준 CPU 처리 시간 약 3~5분(`htdemucs` 기준. `htdemucs_ft`는 그보다 오래 걸림). GPU는
  카드에 따라 다르지만 통상 수 초~수십 초 수준으로 훨씬 빠름.
- 처리 완료/실패 후 로컬 업로드 파일과 Demucs 산출물은 자동 삭제됨(Supabase Storage에만 보관)
- Supabase Storage 정리: `stem-uploads`(원본)는 다운로드 직후 즉시 삭제, `separated-audio`(결과)는
  업로드 후 1시간 지나면, `youtube-audio`/`pitch-speed-audio`(추출/변환 결과)는 업로드 후 15분 지나면 자동
  삭제됨(다시 뽑는 비용이 크지 않아 짧게 잡음). 서버 시작 시 한 번 + 이후 15분마다 정리 스레드가 돎. 보관
  기간은 `main.py`의 `UPLOAD_RETENTION_HOURS`/`RESULT_RETENTION_HOURS`/`YOUTUBE_RETENTION_HOURS`/
  `PITCH_SPEED_RETENTION_HOURS`로, 정리 주기는 `CLEANUP_INTERVAL_SECONDS`로 조절 가능.
- 유튜브 오디오 추출은 `yt-dlp` + FFmpeg(`FFmpegExtractAudio` 후처리)로 mp3 320kbps 변환까지 하므로,
  m4a 디코딩용으로 이미 설정해둔 `FFMPEG_DIR`을 그대로 재사용함(별도 설정 불필요).
- 피치/속도 조절(`rubberband` 필터)은 winget으로 설치한 Gyan.FFmpeg 빌드에 `--enable-librubberband`가
  포함돼 있어서 별도 설치 없이 바로 됨. 다른 FFmpeg 배포판을 쓴다면 rubberband가 빠져있을 수 있으니
  `ffmpeg -filters | findstr rubberband`로 먼저 확인할 것.
