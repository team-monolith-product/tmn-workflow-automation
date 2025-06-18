import os
import re
import hashlib
import base64
import json
import requests

from dotenv import load_dotenv
from notion2md.exporter.block import StringExporter
from notion2md.convertor.block import BLOCK_TYPES, table_row
from markdown import markdown
from notion_client import Client as NotionClient
import pypandoc

load_dotenv()

PAGE_ID = os.environ.get("NOTION_PAGE_ID", "15f1cc820da68063a737f356f8862719")

RAILS_BASE_URL = os.environ.get("RAILS_BASE_URL", "https://class.codle.io")
DIRECT_UPLOAD_PATH = "/api/v1/direct_uploads"

PARTNER_ID = os.environ.get("PARTNER_ID")


def get_notion_md(page_id: str) -> str:
    """노션 페이지를 notion2md로 마크다운 문자열 추출."""
    return StringExporter(block_id=page_id, output_path="dummy").export()


def extract_image_urls(markdown_text: str) -> list:
    """
    마크다운에서 `![...](URL)` 형태의 이미지 링크를 전부 추출.
    """
    pattern = r"!\[.*?\]\((.*?)\)"
    return re.findall(pattern, markdown_text)


def direct_upload_to_rails(file_bytes: bytes, filename: str, content_type: str, rails_bearer_token: str) -> str:
    """
    ActiveStorage Direct Upload 프로세스:
      1) rails에 blob 메타데이터 생성 (POST)
      2) 반환된 URL(S3 등)에 PUT
      3) 최종 /rails/active_storage/blobs/redirect/:signed_id/:filename 형태 URL 반환
    """
    # 1) MD5 & Base64
    byte_size = len(file_bytes)
    md5_digest = hashlib.md5(file_bytes).digest()
    base64_checksum = base64.b64encode(md5_digest).decode("ascii")

    create_blob_url = f"{RAILS_BASE_URL}{DIRECT_UPLOAD_PATH}"
    payload = {
        "blob": {
            "filename": filename,
            "byte_size": byte_size,
            "checksum": base64_checksum,
            "content_type": content_type,
            "metadata": {},
        }
    }
    headers = {
        "Authorization": f"Bearer {rails_bearer_token}",
        "Content-Type": "application/json",
    }

    resp = requests.post(create_blob_url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    direct_upload = data["direct_upload"]
    upload_url = direct_upload["url"]
    upload_headers = direct_upload["headers"]
    signed_id = data["signed_id"]

    # 2) PUT 업로드
    put_resp = requests.put(upload_url, data=file_bytes, headers=upload_headers, timeout=60)
    put_resp.raise_for_status()

    # 3) 최종 Blob 접근 경로
    blob_url = (
        f"{RAILS_BASE_URL}/rails/active_storage/blobs/redirect/{signed_id}/{filename}"
    )
    return blob_url


def replace_images_in_md(md_text: str, rails_bearer_token: str) -> str:
    """
    마크다운 내 모든 이미지 URL을 찾아:
      - URL로부터 다운로드
      - Rails DirectUpload
      - 마크다운 내 URL 치환
    """
    image_urls = extract_image_urls(md_text)
    replaced_md = md_text

    for url in image_urls:
        print(f"[INFO] Download & upload image: {url}")
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                file_bytes = resp.content
                # 파일명 / content-type 추정
                filename_guess = "image.png"
                content_type_guess = resp.headers.get(
                    "Content-Type", "application/octet-stream"
                )

                # 업로드 후 새 URL
                new_url = direct_upload_to_rails(
                    file_bytes, filename_guess, content_type_guess, rails_bearer_token
                )
                replaced_md = replaced_md.replace(url, new_url)
                print(f"   => Replaced with {new_url}")
            else:
                print(f"   => Failed to download (HTTP {resp.status_code})")
        except Exception as e:
            raise e

    return replaced_md


def convert_md_to_html(md_text: str) -> str:
    """
    Python의 markdown 라이브러리로 MD -> HTML 변환
    """
    return markdown(md_text, extensions=["tables"])


def split_markdown_into_two_parts(
    md_text: str, start_heading: str, next_heading: str
) -> tuple[str, str]:
    """
    'start_heading'가 들어간 줄부터
    'next_heading'가 들어간 줄 직전까지를 PART1,
    'next_heading'부터 문서 끝까지를 PART2로 분리.

    예:
      start_heading = "현행 내용"
      next_heading  = "수정 내용"
    """
    lines = md_text.splitlines()
    part1_lines = []
    part2_lines = []

    # 찾을 인덱스 초기화
    start_idx = None
    next_idx = None

    # 각 줄을 순회하며, start_heading, next_heading가 포함된 줄의 인덱스 찾기
    for i, line in enumerate(lines):
        if start_heading in line and start_idx is None:
            start_idx = i
        if next_heading in line and next_idx is None:
            next_idx = i

    if start_idx is None:
        # "start_heading" 문구를 못 찾으면, 오류 발생
        raise ValueError(f"Cannot find '{start_heading}' in the markdown text.")

    # "next_heading"를 못 찾으면 오류 발생생
    if next_idx is None:
        raise ValueError(f"Cannot find '{next_heading}' in the markdown text.")
    else:
        part1_lines = lines[start_idx + 1 : next_idx]
        part2_lines = lines[next_idx + 1 :]

    part1 = "\n".join(part1_lines).strip()
    part2 = "\n".join(part2_lines).strip()

    return (part1, part2)


def get_before_and_after_html(page_id: str = PAGE_ID, rails_bearer_token: str | None = None):
    if rails_bearer_token is None:
        raise ValueError("rails_bearer_token is required")
    # 1) 노션 문서 -> 전체 MD
    original_md = get_notion_md(page_id)

    # 2) "2. 현행 내용" ~ "3. 수정 내용" 으로 문서 분할
    #    (실제 h3 제목에 맞춰 string match)
    part1_md, part2_md = split_markdown_into_two_parts(
        original_md, "현행 내용", "수정 내용"
    )

    # 만약 "현행 내용" ~ "수정 내용" 구간만 따로 떼어내고, "수정 내용"부터 끝까지도 따로 떼어내고 싶다면
    # 위와 같이 split. part1, part2 각각에 대해 아래 절차 진행.

    # 3) 각 파트별로 이미지 업로드 치환 -> HTML 변환
    if part1_md:
        part1_md_replaced = replace_images_in_md(part1_md, rails_bearer_token)
        part1_html = convert_md_to_html(part1_md_replaced)

    if part2_md:
        part2_md_replaced = replace_images_in_md(part2_md, rails_bearer_token)
        part2_html = convert_md_to_html(part2_md_replaced)

    return part1_html, part2_html


def sign_in_to_update_system():
    """
    POST /amdnmg/ame/login/generalLoginAction.do HTTP/1.1
    Content-Type: application/json; charset=UTF-8
    User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36

    REQUEST BODY
    {"uid":"<ID>","bfEcrpPswd":"<PASSWORD>","userId":"","userPw":""}

    RESPONSE HEADER
    Set-Cookie: SESSION=ZjA1OGU1OGItNjEyOC00NTQwLWIxOTAtNjgwZTdmNzk1NjYx; Path=/amdnmg; HttpOnly; SameSite=Lax
    """
    # UPDATE_SYSTEM_ID, UPDATE_SYSTEM_PASSWORD 환경 변수로 로그인
    update_system_id = os.environ.get("UPDATE_SYSTEM_ID")
    update_system_password = os.environ.get("UPDATE_SYSTEM_PASSWORD")

    login_url = "https://mi.aidtbook.kr:8443/amdnmg/ame/login/generalLoginAction.do"
    payload = {
        "uid": update_system_id,
        "bfEcrpPswd": update_system_password,
        "userId": "",
        "userPw": "",
    }
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
    }

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        }
    )
    resp = session.post(login_url, json=payload, headers=headers)
    resp.raise_for_status()

    if session.cookies.get("SESSION", None):
        # 세션 쿠키 반환
        return session
    else:
        return None


def upload_meeting_minutes_to_update_system(session, file_path: str):
    """
    POST https://mi.aidtbook.kr:8443/amdnmg/cm/fileUpload.do
    multipart/form-data; boundary=----WebKitFormBoundaryhaEIFsuO4qnrZvAv

    ------WebKitFormBoundaryhaEIFsuO4qnrZvAv
    Content-Disposition: form-data; name="files"; filename="77758c28-08d6-4734-ac13-33b52bc06e1a_갈림길_현재활동_노출되게_변경_회의록.pdf"
    Content-Type: application/pdf


    ------WebKitFormBoundaryhaEIFsuO4qnrZvAv
    Content-Disposition: form-data; name="fileInfo"

    [{"atchFileGroupId":"","atchFileSn":0,"sysSeCd":"AME","docTypeCd":"","fileNm":"77758c28-08d6-4734-ac13-33b52bc06e1a_갈림길_현재활동_노출되게_변경_회의록.pdf","fileUrlAddr":"","imgYn":"","sysStrgFileNm":"","fileCpcty":217249,"fileExpln":"","sts":"i"}]
    ------WebKitFormBoundaryhaEIFsuO4qnrZvAv--
    fileCpcty: 파일 크기? 실제 업로드 했던 것은 218KB (224,173 바이트) 224KB (229,376 바이트)
               파일 크기 맞는 것 같음 바이너리의 길이를 말하는 것으로 보임

    fetch("https://mi.aidtbook.kr:8443/amdnmg/cm/fileUpload.do", {
    "headers": {
        "accept": "*/*",
        "accept-language": "ko,en;q=0.9,de;q=0.8,ja;q=0.7",
        "content-type": "multipart/form-data; boundary=----WebKitFormBoundaryhaEIFsuO4qnrZvAv",
        "menuid": "AME000104",
        "pgmid": "MDF001P2",
        "sec-ch-ua": "\"Google Chrome\";v=\"131\", \"Chromium\";v=\"131\", \"Not_A Brand\";v=\"24\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-requested-with": "XMLHttpRequest"
    },
    "referrer": "https://mi.aidtbook.kr:8443/amdnmg/index.do",
    "referrerPolicy": "strict-origin-when-cross-origin",
    "body": "------WebKitFormBoundaryhaEIFsuO4qnrZvAv\r\nContent-Disposition: form-data; name=\"files\"; filename=\"77758c28-08d6-4734-ac13-33b52bc06e1a_갈림길_현재활동_노출되게_변경_회의록.pdf\"\r\nContent-Type: application/pdf\r\n\r\n\r\n------WebKitFormBoundaryhaEIFsuO4qnrZvAv\r\nContent-Disposition: form-data; name=\"fileInfo\"\r\n\r\n[{\"atchFileGroupId\":\"\",\"atchFileSn\":0,\"sysSeCd\":\"AME\",\"docTypeCd\":\"\",\"fileNm\":\"77758c28-08d6-4734-ac13-33b52bc06e1a_갈림길_현재활동_노출되게_변경_회의록.pdf\",\"fileUrlAddr\":\"\",\"imgYn\":\"\",\"sysStrgFileNm\":\"\",\"fileCpcty\":217249,\"fileExpln\":\"\",\"sts\":\"i\"}]\r\n------WebKitFormBoundaryhaEIFsuO4qnrZvAv--\r\n",
    "method": "POST",
    "mode": "cors",
    "credentials": "include"
    });

    RESPONSE
    {
        "fileList": [
            {
                "delYn": "",
                "frstRegUid": "",
                "frstRegDt": "",
                "lastMdfcnUid": "",
                "lastMdfcnDt": "",
                "atchFileGroupId": "AMEee261bc4-aff5-4e61-afce-6cdb211d2991",
                "atchFileSn": 1,
                "sysSeCd": "AME",
                "fileNm": "77758c28-08d6-4734-ac13-33b52bc06e1a_갈림길_현재활동_노출되게_변경_회의록.pdf",
                "fileUrlAddr": "/nas_data/ame/AME/2025/01/22",
                "imgYn": "N",
                "sysStrgFileNm": "94ea6865-cd3d-4457-a916-d24c46d43994.pdf",
                "fileCpcty": 217249,
                "useYn": "Y"
            }
        ]
    }
    """
    upload_url = "https://mi.aidtbook.kr:8443/amdnmg/cm/fileUpload.do"
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)

    headers = {
        "Accept": "*/*",
        "pgmid": "MDF001P2",
        "menuid": "AME000104",
    }
    files = {
        "files": (
            file_name,
            open(file_path, "rb"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    }
    # fileInfo -> multipart의 텍스트 파트 (JSON 문자열로 넘겨주는 게 안전)
    file_info_value = json.dumps(
        [
            {
                "atchFileGroupId": "",
                "atchFileSn": 0,
                "sysSeCd": "AME",
                "docTypeCd": "",
                "fileNm": file_name,
                "fileUrlAddr": "",
                "imgYn": "",
                "sysStrgFileNm": "",
                "fileCpcty": file_size,
                "fileExpln": "",
                "sts": "i",
            }
        ]
    )
    data = {"fileInfo": file_info_value}

    resp = session.post(upload_url, headers=headers, files=files, data=data)
    resp.raise_for_status()
    data = resp.json()
    print(data)

    return data["fileList"][0]["atchFileGroupId"]


def upload_to_update_system(
    session,
    atch_file_group_id: str,
    before_html: str,
    after_html: str,
    keyword: str,
    access_path_explain: str,
    deployment_date: str,
):
    """
    fetch("https://mi.aidtbook.kr:8443/amdnmg/ame/mdfMng/saveMdfcnSplmntComm.do", {
    "headers": {
        "accept": "*/*",
        "accept-language": "ko,en;q=0.9,de;q=0.8,ja;q=0.7",
        "content-type": "application/json; charset=UTF-8",
        "menuid": "AME000104",
        "pgmid": "MDF009M0",
        "sec-ch-ua": "\"Google Chrome\";v=\"131\", \"Chromium\";v=\"131\", \"Not_A Brand\";v=\"24\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-requested-with": "XMLHttpRequest"
    },
    "referrer": "https://mi.aidtbook.kr:8443/amdnmg/index.do",
    "referrerPolicy": "strict-origin-when-cross-origin",
    "method": "POST",
    "mode": "cors",
    "credentials": "include"
    });

    REQUEST BODY
    [
        {
            "mdspSn": "",
            "mdspDmndNo": "",
            "prtnrId": "9502d72f-0970-50fc-9370-212ba9a0e9e0", // 파트너 ID이므로 상수
            "txbkMnchNm": "[기술] 기능 개선", // 1차 업로드에서는 상수
            "txbkMdchNm": "서비스 개선", // 1차 업로드에서는 상수
            "acsUrlAddr": "",
            "txbkNowCn": "<ul>\n<li>\n<p>SNB에서 처음에는 기본으로 첫번째 레벨의 갈림길이 선택되어 있어서 다른 레벨의 활동은 노출되지 않았습니다. </p>\n<p><img alt=\"image.png\" src=\"https://class.codle.io/rails/active_storage/blobs/redirect/eyJfcmFpbHMiOnsibWVzc2FnZSI6IkJBaHBBczlNIiwiZXhwIjpudWxsLCJwdXIiOiJibG9iX2lkIn19--a485f946e0be43fab505b5ade89034db2b930766/image.png\"></p>\n</li>\n</ul>",
            "txbkCrctCn": "<ul>\n<li>\n<p>현재 활동이 속한 레벨의 갈림길이 선택되어 있도록 변경했습니다.</p>\n<p><img alt=\"image.png\" src=\"https://class.codle.io/rails/active_storage/blobs/redirect/eyJfcmFpbHMiOnsibWVzc2FnZSI6IkJBaHBBdEJNIiwiZXhwIjpudWxsLCJwdXIiOiJibG9iX2lkIn19--41e80f8fa8706625ce8338bd6985a2b20c203a5a/image.png\"></p>\n</li>\n</ul>",
            "nowCnAtchFileId": "",
            "crctCnAtchFileId": "",
            "kywdCn": "SNB 갈림길 현재 활동 노출 ", // 키워드
            "crctTypeCd": "C3", // 아마 교과서나 단원일 듯. 지금은 상수
            "crctArtclTypeCd": "C302", // 아마 교과서나 단원일 듯. 지금은 상수
            "crctRsnCd": "R2", // 아마 교과서나 단원일 듯. 지금은 상수
            "crctRvwRqstrSeCd": "R201", // 아마 교과서나 단원일 듯. 지금은 상수
            "rvwRqstrRmrk": "",
            "rfltYmd": "20250205", // 배포 일자
            "autCnsltnFileId": "AMEee261bc4-aff5-4e61-afce-6cdb211d2991", // 회의록 ID 직전 POST https://mi.aidtbook.kr:8443/amdnmg/cm/fileUpload.do 에서 획득 가능
            "mdspRegYmd": "",
            "mdspRegUid": "",
            "mdIdntyYmd": "",
            "mdIdntyUid": "",
            "mdspPrcsSttsCd": "",
            "mdfcnSplmntPrcsSttsNm": "",
            "schlCrsSeNm": "",
            "sbjtgrpNm": "",
            "sbjctNm": "정보", // 상수
            "autNm": "김영일", // 상수
            "dvlpcNm": "금성출판사", // 상수
            "mdfYn": "N", // ? 뭔지 모르겠지만 뭐 동의했냐 일 듯
            "txbkPblcnTypeCd": "02", // 텍스트북 퍼블리케이션 타입 코드: 아마 정보 = 02 일듯
            "acsPathExpln": "[교실 코스] - [차례] - [갈림길]", // 접근 경로
            "schlvSeCd": "3", // school level section code? 아마  3= 고등
            "schlvSbjtgrpSeCd": "303", // 모르겠지만 상수로; 303 = 고등 정보일듯
            "sMulYn": "",
            "sts": "i" // status 일 것 같고, 등록 후 대기 상태일듯?
        }
    ]

    """
    url = "https://mi.aidtbook.kr:8443/amdnmg/ame/mdfMng/saveMdfcnSplmntComm.do"
    headers = {
        "content-type": "application/json; charset=UTF-8",
        "menuid": "AME000104",
        "pgmid": "MDF009M0",
    }
    payload = [
        {
            "mdspSn": "",
            "mdspDmndNo": "",
            "prtnrId": PARTNER_ID,
            "txbkMnchNm": "[기술] 기능 개선",
            "txbkMdchNm": "서비스 개선",
            "acsUrlAddr": "",
            "txbkNowCn": before_html,
            "txbkCrctCn": after_html,
            "nowCnAtchFileId": "",
            "crctCnAtchFileId": "",
            "kywdCn": keyword,
            "crctTypeCd": "C3",
            "crctArtclTypeCd": "C302",
            "crctRsnCd": "R2",
            "crctRvwRqstrSeCd": "R201",
            "rvwRqstrRmrk": "",
            "rfltYmd": deployment_date,
            "autCnsltnFileId": atch_file_group_id,
            "mdspRegYmd": "",
            "mdspRegUid": "",
            "mdIdntyYmd": "",
            "mdIdntyUid": "",
            "mdspPrcsSttsCd": "",
            "mdfcnSplmntPrcsSttsNm": "",
            "schlCrsSeNm": "",
            "sbjtgrpNm": "",
            "sbjctNm": "정보",
            "autNm": "김영일",
            "dvlpcNm": "금성출판사",
            "mdfYn": "N",
            "txbkPblcnTypeCd": "02",
            "acsPathExpln": access_path_explain,
            "schlvSeCd": "3",
            "schlvSbjtgrpSeCd": "303",
            "sMulYn": "",
            "sts": "i",
        }
    ]

    resp = session.post(url, json=payload, headers=headers)
    resp.raise_for_status()

    return resp.json()


def list_update(session, title):
    url = "https://mi.aidtbook.kr:8443/amdnmg/ame/mdfMng/selectMdfcnSplmntList.do"
    headers = {
        "content-type": "application/json; charset=UTF-8",
    }
    payload = {
        "curPage": "1",
        "pageSize": "40",
        "totalCnt": "1",
        "sSchlvSeCd": "",
        "sSchlvSbjtgrpSeCd": "정보",
        "sSbjctNm": "",
        "sAutNm": "",
        "sTxbkMnchNm": "",
        "sTxbkMdchNm": "",
        "sKywdCn": title,
    }
    resp = session.post(url, json=payload, headers=headers)
    resp.raise_for_status()

    return resp.json()


# 전역 변수로 rails_bearer_token 저장
_current_rails_bearer_token = None

def set_rails_bearer_token(token: str):
    global _current_rails_bearer_token
    _current_rails_bearer_token = token

def video(info: dict):
    global _current_rails_bearer_token
    file_name = info["file_name"]
    url = info["url"]

    # file_name이 mp4 등 비디오 파일이라면
    if file_name.endswith(".mp4") or file_name.endswith(".mov"):
        print(f"[INFO] Download & upload video: {url}")
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            file_bytes = resp.content
            # 파일명 / content-type 추정
            content_type_guess = resp.headers.get(
                "Content-Type", "application/octet-stream"
            )

            # 업로드 후 새 URL
            try:
                if _current_rails_bearer_token is None:
                    print("[ERROR] Rails bearer token is required for video upload")
                    return ""
                new_url = direct_upload_to_rails(file_bytes, file_name, content_type_guess, _current_rails_bearer_token)
            except Exception as e:
                print(f"[ERROR] Failed to upload video: {e}")
            return f'<video width="600" controls><source src="{new_url}"/>'
        else:
            # raise 가 무시되는 듯 함.
            # raise ValueError(f"Failed to download (HTTP {resp.status_code})")
            print(f"[ERROR] Failed to download video (HTTP {resp.status_code})")
    else:
        # raise 가 무시되는 듯 함.
        # raise ValueError(f"Unsupported video format: {file_name}")
        print(f"[ERROR] Unsupported video format: {file_name}")


def new_table_row(info: list) -> list:
    old = table_row(info)
    # \n를 <br/>로 치환
    return [o.replace("\n", "<br/>") for o in old]


BLOCK_TYPES["video"] = video
BLOCK_TYPES["table_row"] = new_table_row


def main():
    notion = NotionClient(auth=os.environ["NOTION_TOKEN"])
    # 1. mdspSn 속성이 없는 모든 문서를 조회
    print("수정/보완 시스템에 업로드되지 않은 문서를 조회합니다...")
    database_id = "15a1cc820da68092af44fa0d2975cba4"

    response = notion.databases.query(
        **{
            "database_id": database_id,
            "filter": {"property": "mdspSn", "rich_text": {"is_empty": True}},
        }
    )

    # 수정/보완 시스템 로그인
    print("수정/보완 시스템에 로그인합니다...")
    session = sign_in_to_update_system()
    if not session:
        print("로그인에 실패했습니다.")
        return

    deployment_date = input("배포 일자를 입력하세요 (YYYYMMDD): ")
    rails_bearer_token = input("Rails Bearer Token을 입력하세요: ")
    
    # Rails Bearer Token을 전역 변수로 설정
    set_rails_bearer_token(rails_bearer_token)
    
    for item in response["results"]:
        title = item["properties"]["제목"]["title"][0]["plain_text"]
        print(f"'{title}' 문서에 대해 처리를 시작합니다.")

        access_path_explain = item["properties"]["상세 위치 (⚠️필수)"]["rich_text"][0][
            "plain_text"
        ]

        # 2. 회의록을 DOCX로 변환환
        os.makedirs("meeting_minutes", exist_ok=True)

        meeting_minutes_id = item["properties"]["회의록"]["relation"][0]["id"]
        meeting_minutes_md = StringExporter(
            block_id=meeting_minutes_id, output_path="dummy"
        ).export()
        pypandoc.convert_text(
            meeting_minutes_md,  # 변환할 원본(문자열)
            "docx",  # 목표 포맷
            format="md",  # 원본의 포맷(Markdown)
            outputfile=f"meeting_minutes/{title} 회의록.docx",
        )
        print(f"DOCX 파일이 생성되었습니다: meeting_minutes/{title} 회의록.docx")

        # 3. 노션 문서를 HTML로 변환
        before_html, after_html = get_before_and_after_html(item["id"], rails_bearer_token)

        print("업로드 준비가 완료되었습니다. 다음 내용을 확인해주세요.")
        print(f"키워드: {title}")
        print(f"상세 위치: {access_path_explain}")
        print("=== BEFORE ===")
        print(before_html)
        print("=== AFTER ===")
        print(after_html)
        print("== 회의록 ===")
        print(f"meeting_minutes/{title} 회의록.docx")

        yn = input("처리를 진행하시겠습니까? (y/n/k): ")
        if yn.lower() == "k":
            print("처리를 중단합니다.")
            break
        if yn.lower() != "y":
            print(f"{title}에 대한 처리를 건너뜁니다.")
            continue

        # DOCX 파일을 업로드하고, 파일 ID를 획득
        print("DOCX 파일을 수정/보완 시스템에 업로드합니다...")
        atch_file_group_id = upload_meeting_minutes_to_update_system(
            session, f"meeting_minutes/{title} 회의록.docx"
        )
        print(
            f"DOCX 파일이 수정/보완 시스템에 업로드되었습니다. ID: {atch_file_group_id}"
        )

        # 수정/보완 시스템에 업로드
        print(f"'{title}' 건을 수정/보완 시스템에 업로드합니다...")

        upload_to_update_system(
            session,
            atch_file_group_id,
            before_html,
            after_html,
            title,
            access_path_explain,
            deployment_date,
        )
        print(f"'{title}' 건이 수정/보완 시스템에 업로드되었습니다.")

        print(f"'{title}' 건을 수정/보완 시스템에서 검색합니다...")
        update_list = list_update(session, title)

        print(update_list)

        found = False
        for i in update_list["dsList"]:
            if i["kywdCn"] == title:
                print(f"'{title}' 건이 수정/보완 시스템에 정상 등록되었습니다.")
                # notion 페이지에 mdspSn 업데이트
                notion.pages.update(
                    page_id=item["id"],
                    properties={
                        "mdspSn": {
                            "rich_text": [
                                {"type": "text", "text": {"content": i["mdspSn"]}}
                            ]
                        }
                    },
                )
                found = True
                break

        if not found:
            print(f"'{title}' 건이 수정/보완 시스템에 등록되지 않았습니다.")
            print("수동으로 확인해주세요.")
            break


if __name__ == "__main__":
    main()
