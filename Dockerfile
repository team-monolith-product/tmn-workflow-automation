# 어드민 프론트(WA 어드민 SPA) 빌드 스테이지 — 산출물(dist)을 prd 이미지로 복사한다.
FROM node:22-slim AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# 베이스 이미지 설정
FROM python:3.11-slim as prd

# 환경 변수 설정 (Python 출력 버퍼링 비활성화)
ENV PYTHONUNBUFFERED=1

# 작업 디렉토리 설정
WORKDIR /app

# 한글 폰트 설치 (matplotlib 차트 한글 표시용)
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-nanum \
    git \
    && rm -rf /var/lib/apt/lists/*

# matplotlib 폰트 캐시 삭제 (새 폰트 인식을 위해)
RUN rm -rf /root/.cache/matplotlib

# 필요한 패키지 설치
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 코드 복사
COPY . .

# 어드민 SPA 빌드 산출물 복사 (main.py 가 /admin 으로 서빙)
COPY --from=frontend /fe/dist ./frontend/dist

# 애플리케이션 실행 (FastAPI 배포는 helm 에서 uvicorn main:app 으로 override)
CMD ["python", "app.py"]
