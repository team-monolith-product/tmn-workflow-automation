"""Gmail push 유실을 보완하는 변경분 동기화 작업입니다."""

import asyncio

from service.recovery_mail import RecoveryMailSyncBusy, sync_recovery_mail


def main() -> None:
    try:
        result = asyncio.run(sync_recovery_mail())
    except RecoveryMailSyncBusy:
        print("[recovery-mail] 다른 동기화가 실행 중이어서 건너뜁니다.")
        return
    if result is None:
        print("[recovery-mail] 비활성화 상태입니다.")
        return
    print(
        "[recovery-mail] 동기화 완료: "
        f"검사 {result.inspected}건, 전달 {result.forwarded}건"
    )


if __name__ == "__main__":
    main()
