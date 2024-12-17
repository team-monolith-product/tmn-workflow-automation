"""
슬랙 봇을 위한 FastAPI 서버
"""
from fastapi import FastAPI

app = FastAPI()

@app.get("/bug")
def bug():
    """
    버그 리포트를 위한 API
    """
    return {"Hello": "World"}
