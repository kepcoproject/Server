# 스마트 에너지 절약 시스템 백엔드
#
# 로컬 시연에는 필요 없다. Render·Railway·Fly 같은 곳에 올릴 때 쓴다.
FROM python:3.12-slim

WORKDIR /app

# 의존성을 먼저 넣어 캐시를 살린다 (코드만 바뀌면 재설치하지 않는다)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# 호스팅 업체는 대개 PORT 환경변수로 포트를 지정한다. 없으면 8000.
ENV PORT=8000
EXPOSE 8000

# --workers 는 1로 둔다. MQTT 구독이 워커마다 붙으면 같은 메시지를
# 여러 번 저장하고 웹훅도 중복 발송된다.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
