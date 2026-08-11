import sys

sys.path.insert(
    0,
    "/Users/baeyeongbin/Workspace/repo/ai/tmn-workflow-automation/.claude/worktrees/sms-core",
)

from service.sms import ledger, templates
from tests.fakes_sheets import FakeWorksheet

print("HEADER len =", len(ledger.HEADER))

ws = FakeWorksheet([ledger.HEADER])
won, lost = ledger.claim(
    ws,
    "discord",
    [{"to": "01011111111", "name": "홍길동"}],
    "LMS",
    "a@team-mono.com",
    "slack",
)
written = ws.rows[1]
print("written row len =", len(written))
print("written row =", written)
row = ledger.read_rows(ws)[0]
print("요청자 =", repr(row.get("요청자")))
print("경로 =", repr(row.get("경로")))
print()

# 사람이 하이픈 붙여 손으로 적은 줄
ws2 = FakeWorksheet([ledger.HEADER])
ws2.rows.append(
    [
        "2026-08-11",
        "discord",
        "010-1111-1111",
        "홍길동",
        "LMS",
        "",
        "1000",
        "형관",
        "manual",
    ]
)
won2, lost2 = ledger.claim(
    ws2,
    "discord",
    [{"to": templates.normalize_phone("010-1111-1111")}],
    "LMS",
    "a@b.c",
    "script",
)
print("하이픈 손기록 있는데 won =", won2, "lost =", lost2)
print()

print("resolve(None, '') =", repr(templates.resolve(None, "")))
try:
    templates.resolve("", None)
except Exception as e:
    print("resolve('', None) ->", type(e).__name__, e)
