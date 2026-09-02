"""Gmail mailbox watch를 갱신합니다."""

import asyncio

from service.recovery_mail import RecoveryMailSyncBusy, renew_recovery_mail_watch


def main() -> None:
    try:
        watch = asyncio.run(renew_recovery_mail_watch())
    except RecoveryMailSyncBusy:
        print("[recovery-mail] 다른 동기화가 실행 중이어서 갱신을 건너뜁니다.")
        return
    if watch is None:
        print("[recovery-mail] 비활성화 상태입니다.")
        return
    print(f"[recovery-mail] Gmail 감시 갱신 완료: 만료 시각 {watch.expiration}")


if __name__ == "__main__":
    main()
