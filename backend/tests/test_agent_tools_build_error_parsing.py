"""解析规则覆盖测试：Gradle 配置期/系统级错误行必须被结构化。

现有解析器只覆盖编译器错误格式（kotlinc e:/javac/AAPT2 error: 前缀），
Gradle 自己的错误句式（Task not found / Execution failed / Script
compilation error 等）此前全部落入 unrecognized_lines，甚至漏计。
这些行 file=""（无文件定位），message=整行。
"""

from app.services.agent_tools import _parse_android_build_errors


def _single_error(output: str) -> list[str]:
    summary = _parse_android_build_errors(output)
    return [e.message for e in summary.errors]


def test_task_not_found_is_structured():
    line = "Task 'testDebugUnitTest lintDebug' not found in root project 'PinjamanIndonesia' and its subprojects."
    summary = _parse_android_build_errors(line)

    assert len(summary.errors) == 1
    assert summary.errors[0].file == ""
    assert "testDebugUnitTest lintDebug" in summary.errors[0].message
    # 一旦结构化，不再计入 unrecognized。
    assert summary.unrecognized_lines == 0


def test_execution_failed_for_task_is_structured():
    line = "Execution failed for task ':app:compileDebugKotlin'."
    summary = _parse_android_build_errors(line)

    assert len(summary.errors) == 1
    assert summary.errors[0].file == ""
    assert ":app:compileDebugKotlin" in summary.errors[0].message


def test_script_compilation_error_is_structured():
    line = "Script compilation error:"
    summary = _parse_android_build_errors(line)

    assert len(summary.errors) == 1
    assert "Script compilation error" in summary.errors[0].message


def test_problem_occurred_configuring_is_structured():
    line = "A problem occurred configuring project ':app'."
    summary = _parse_android_build_errors(line)

    assert len(summary.errors) == 1
    assert ":app" in summary.errors[0].message


def test_environment_errors_are_structured_not_silently_dropped():
    # 环境类错误（锁超时/无法启动/磁盘满/元数据损坏）不是代码错，但必须让
    # 模型看到，否则它会误判为代码错误去改源码。
    lines = [
        "Timeout waiting to lock artifact cache (/home/builduser/.gradle/caches/modules-2). It is currently in use by another Gradle instance.",
        "Gradle could not start your build.",
        "Could not read workspace metadata from /workspace/.gradle/8.7/dependencies-accessors/77e7/metadata.bin",
        "java.io.IOException: No space left on device",
    ]
    for line in lines:
        summary = _parse_android_build_errors(line)
        assert summary.errors, f"未被结构化的环境错误行: {line}"
        assert summary.errors[0].file == ""


def test_mixed_output_keeps_compiler_errors_and_adds_gradle_errors():
    output = "\n".join(
        [
            "e: file:///workspace/app/src/Main.kt:19:35 Unresolved reference: HorizontalDivider",
            "Execution failed for task ':app:compileDebugKotlin'.",
            "Task 'assembleDebug testDebugUnitTest' not found in root project 'LoanApp' and its subprojects.",
        ]
    )
    summary = _parse_android_build_errors(output)

    # 1 条 Kotlin 编译器错误（file 非空）+ 2 条 Gradle 系统错误（file 空）
    assert len(summary.errors) == 3
    kt_errors = [e for e in summary.errors if e.file]
    sys_errors = [e for e in summary.errors if not e.file]
    assert len(kt_errors) == 1
    assert "HorizontalDivider" in kt_errors[0].message
    assert len(sys_errors) == 2


def test_format_android_build_failure_prompts_batch_fix() -> None:
    """回归：失败文案必须引导一次性修复全部错误（逐条往返浪费编译轮次）。"""
    from app.services.agent_tools import _BuildError, _BuildErrorSummary, _format_android_build_failure

    errs = _BuildErrorSummary()
    errs.errors = [
        _BuildError(category="error", file="a.kt", line=1, column=None, message="Unresolved reference 'X'."),
        _BuildError(category="error", file="b.kt", line=2, column=None, message="Unresolved reference 'Y'."),
    ]
    text = _format_android_build_failure(1, "x", errs, 1000, gradle_task="assembleDebug")
    assert "一次性修复" in text
    assert "全部错误" in text
