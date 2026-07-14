# 빌드 도구(git 등)는 builder 단계에만 두고, 런타임 이미지(prd)에는 실행에 필요한 것만
# 남겨 OS 패키지 CVE 노출 면적을 줄인다. git 이 끌어오는 libssh2·perl·libcurl·libtasn1 은
# 런타임에서 쓰지 않으므로 최종 이미지에서 제외된다.

# ===== Builder Stage =====
FROM python:3.14-slim@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015 AS builder

WORKDIR /app

# git: requirements.txt 의 `notion-to-md-py @ git+https://...` 설치에 필요 (빌드 타임 전용)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# 의존성을 격리된 venv 에 설치하여 런타임 단계로 통째로 복사한다.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ===== Production Stage =====
FROM python:3.14-slim@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015 AS prd

# 환경 변수 설정 (Python 출력 버퍼링 비활성화)
ENV PYTHONUNBUFFERED=1

# 작업 디렉토리 설정
WORKDIR /app

# 한글 폰트 설치 (matplotlib 차트 한글 표시용)
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-nanum \
    && rm -rf /var/lib/apt/lists/*

# matplotlib 폰트 캐시 삭제 (새 폰트 인식을 위해)
RUN rm -rf /root/.cache/matplotlib

# builder 에서 설치한 의존성(venv) 복사
ENV PATH="/opt/venv/bin:$PATH"
COPY --from=builder /opt/venv /opt/venv

# 애플리케이션 코드 복사
COPY . .

# 애플리케이션 실행
CMD ["python", "app.py"]
