import pathlib
import sys
import tempfile

WT = "/Users/baeyeongbin/Workspace/repo/ai/tmn-workflow-automation/.claude/worktrees/sms-core"
sys.path.insert(0, WT)
sys.path.insert(0, WT + "/scripts")

import importlib.util

spec = importlib.util.spec_from_file_location("send_sms", WT + "/scripts/send_sms.py")
send_sms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(send_sms)

from service.sms import send as sms_send

csv_text = "to,name,var1\n,홍길동,1기\n010-2222-2222,김철수,2기\n"
with tempfile.NamedTemporaryFile(
    "w", suffix=".csv", delete=False, encoding="utf-8"
) as f:
    f.write(csv_text)
    path = pathlib.Path(f.name)

rows = send_sms.read_csv(path)
print("read_csv ->", rows)
try:
    problems = sms_send.check(rows, template_name="discord")
    print("check ->", problems)
except Exception as e:
    print("check 폭발 ->", type(e).__name__, e)

print()
# 빈 문자열이 살아 있었다면?
rows2 = [{"to": "", "name": "홍길동", "var1": "1기"}, {"to": "010-2222-2222"}]
print("필터 없었다면 check ->", sms_send.check(rows2, template_name="discord"))

print()
# --content "" 로 빈 본문 발송이 통과하는가
print("check(빈 본문) ->", sms_send.check([{"to": "010-1111-1111"}], content=""))
print("preview(빈 본문) ->", sms_send.preview([{"to": "010-1111-1111"}], None, ""))
