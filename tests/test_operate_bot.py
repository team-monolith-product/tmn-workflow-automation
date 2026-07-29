"""
Operate Bot 시스템 프롬프트 테스트
"""

from app import operate_bot


class TestSystemPromptWorkspaceSection:
    """작업 공간이 지정됐을 때만 관련 안내가 붙는다"""

    def test_workspace_section_absent_when_unscoped(self):
        prompt = operate_bot._build_system_prompt(workspace_scoped=False)
        assert "Drive 작업 공간" not in prompt

    def test_workspace_section_present_when_scoped(self):
        prompt = operate_bot._build_system_prompt(workspace_scoped=True)
        assert "Drive 작업 공간" in prompt
        assert "범위 밖" in prompt

    def test_base_instructions_are_always_kept(self):
        for scoped in (True, False):
            prompt = operate_bot._build_system_prompt(workspace_scoped=scoped)
            assert "search_drive_files" in prompt
            assert "create_ops_task" in prompt
