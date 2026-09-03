# 스마트 에너지 절약 시스템 백엔드
#
# 프론트엔드(kepcoproject/Client)를 함께 빌드해 한 주소에서 서빙한다.
# 학교 서버처럼 포트를 하나만 쓸 수 있는 환경을 염두에 둔 구성이다.
#
# 화면 경로(/spaces, /devices, /users)가 API 경로와 겹치므로 API 는
# COMPAT_API_PREFIX 아래로 내리고, 프론트도 같은 값을 보도록 빌드한다.
# 두 값이 어긋나면 로그인부터 404 가 나므로 여기서 한 번에 맞춘다.

# ---------------------------------------------------------------------------
# 1단계 — 프론트엔드 빌드
# ---------------------------------------------------------------------------
# slim 이 아니라 기본 이미지를 쓴다. git 이 이미 들어 있어 apt-get 이 필요 없다.
# 학교망처럼 데비안 패키지 서버가 막힌 환경에서는 apt-get 이 exit 100 으로 실패한다.
# 빌드 단계라 이미지가 커져도 최종 이미지 크기에는 영향이 없다.
FROM node:20 AS frontend

ARG CLIENT_REPO=https://github.com/kepcoproject/Client
ARG CLIENT_REF=main
# 백엔드의 COMPAT_API_PREFIX 와 반드시 같아야 한다.
ARG API_PREFIX=/client-api

WORKDIR /build
RUN git clone --depth 1 --branch ${CLIENT_REF} ${CLIENT_REPO} .

RUN npm ci --no-audit --no-fund

# 같은 서버에서 서빙하므로 절대 주소가 아니라 접두사만 준다.
# 목업은 끈다 — 진짜 백엔드를 보게 해야 한다.
ENV VITE_API_BASE_URL=${API_PREFIX}
ENV VITE_USE_MOCKS=false
# --base=/ 로 덮어쓴다. 프론트 저장소는 GitHub Pages(/Client/)를 기본으로 두고 있는데,
# 여기서는 서버 루트에서 서빙하므로 그대로 두면 자산 경로가 어긋나 흰 화면만 나온다.
RUN npx vite build --base=/

# ---------------------------------------------------------------------------
# 2단계 — 백엔드
# ---------------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

# 의존성을 먼저 넣어 캐시를 살린다 (코드만 바뀌면 재설치하지 않는다)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# 1단계에서 만든 화면. app/main.py 가 FRONTEND_DIR 에서 찾는다.
COPY --from=frontend /build/dist ./web

# 호스팅 업체는 대개 PORT 환경변수로 포트를 지정한다. 없으면 8000.
ENV PORT=8000
EXPOSE 8000

# --workers 는 1로 둔다. MQTT 구독이 워커마다 붙으면 같은 메시지를
# 여러 번 저장하고 웹훅도 중복 발송된다.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
