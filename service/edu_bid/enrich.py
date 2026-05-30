"""
S4 보강 — 숏리스트 공고의 규격서(제안요청서·과업·공고문) 첨부를 받아 텍스트로 추출.

PDF는 PyMuPDF(fitz), HWP/HWPX는 gethwp 로 추출하고, HWP 레코드 태그가 섞이는
노이즈를 한글/ASCII 기준으로 정제한다. 첨부는 길어서 토큰이 크므로 숏리스트에만 적용한다.
"""

import re
import zipfile
import tempfile
from pathlib import Path

import requests

from .schemas import Announcement

CHAR_BUDGET = 12000  # 공고 1건당 규격서 본문 상한(문자)
_DOWNLOAD_TIMEOUT = 40

# 정독 우선순위 — 과업/요구가 담긴 문서를 먼저
_PRIORITY_HINTS = ["제안요청서", "과업", "규격", "사양", "사업", "제안서", "공고"]

# 한글/ASCII/숫자/공백/일반기호만 남긴다(HWP 레코드 태그 깨짐 = CJK 한자영역 제거)
_KEEP_RE = re.compile(r"[^가-힣ᄀ-ᇿ㄰-㆏\x09\x0a\x0d\x20-\x7E·…※○●「」『』【】（）]")


def clean_text(text: str) -> str:
    text = _KEEP_RE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _extract_pdf(content: bytes) -> str:
    import fitz

    doc = fitz.open(stream=content, filetype="pdf")
    return "\n".join(page.get_text() for page in doc)


def _extract_hwp(content: bytes, suffix: str) -> str:
    from gethwp import read_hwp, read_hwpx

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as f:
        f.write(content)
        f.flush()
        return read_hwpx(f.name) if suffix == ".hwpx" else read_hwp(f.name)


def extract_text(content: bytes, name: str) -> str:
    """첨부 바이트 → 정제 텍스트. 지원: pdf, hwp, hwpx, zip(내부 재귀)."""
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        return clean_text(_extract_pdf(content))
    if ext in (".hwp", ".hwpx"):
        return clean_text(_extract_hwp(content, ext))
    if ext == ".zip":
        parts = []
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=True) as f:
            f.write(content)
            f.flush()
            with zipfile.ZipFile(f.name) as z:
                for inner in z.namelist():
                    if Path(inner).suffix.lower() in (".pdf", ".hwp", ".hwpx"):
                        parts.append(extract_text(z.read(inner), inner))
        return "\n".join(parts)
    return ""  # 지원 외 포맷(xlsx 등)은 건너뜀


def _download(url: str, session) -> bytes:
    http = session or requests
    resp = http.get(url, timeout=_DOWNLOAD_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def _ranked_docs(spec_docs: list[dict]) -> list[dict]:
    def score(doc):
        nm = doc.get("name", "")
        for i, hint in enumerate(_PRIORITY_HINTS):
            if hint in nm:
                return i
        return len(_PRIORITY_HINTS)

    return sorted(spec_docs, key=score)


def enrich(ann: Announcement, session=None, char_budget: int = CHAR_BUDGET) -> str:
    """공고의 규격서 첨부를 우선순위대로 받아 텍스트로 합친다(예산 내).

    개별 첨부의 다운로드·파싱 실패는 로그 후 건너뛴다(보강은 best-effort).
    """
    chunks: list[str] = []
    used = 0
    for doc in _ranked_docs(ann.spec_docs):
        if used >= char_budget:
            break
        name, url = doc.get("name", ""), doc.get("url", "")
        if not url:
            continue
        try:
            text = extract_text(_download(url, session), name)
        except Exception as exc:  # 외부 파일 — 실패해도 본 평가는 진행
            print(
                f"[edu-bid] 규격서 파싱 실패 {name or url}: {type(exc).__name__} {exc}"
            )
            continue
        if not text:
            continue
        remain = char_budget - used
        snippet = text[:remain]
        chunks.append(f"[{name}]\n{snippet}")
        used += len(snippet)
    return "\n\n".join(chunks)
