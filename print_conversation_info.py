# daily_scrum.py

import random
from datetime import datetime
from dotenv import load_dotenv

from apis.slack import get_conversation_info

# 환경 변수 로드
load_dotenv()

# Slack 채널 ID
SLACK_CHANNEL_ID = 'C02JX95U7AP'

def print_conversation_info():
    # 채널 정보 가져오기
    print(get_conversation_info(SLACK_CHANNEL_ID))

if __name__ == "__main__":
    print_conversation_info()
