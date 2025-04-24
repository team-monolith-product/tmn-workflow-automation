from typing import Any

class PullRequest:
    pass

# 올바른 방법 (대문자 Any)
def correct_function(pr: PullRequest, comments: list[dict[str, Any]]) -> None:
    pass

# 틀린 방법 (소문자 any)
def wrong_function(pr: PullRequest, comments: list[dict[str, any]]) -> None:
    pass