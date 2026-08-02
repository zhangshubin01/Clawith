"""测试 _parse_android_build_errors 的幂等性和状态隔离。

测试项目:
1. 相同输入 100 次调用产生一致输出
2. _BuildErrorSummary list 默认值每次新建实例
3. 空/None/畸形输入连续调用不累积错误状态
4. 模块级正则对象不被意外修改
5. 两次独立解析结果不互相影响
6. frozen=True dataclass 实例不可修改
"""

import dataclasses
import sys

sys.path.insert(0, "/app")

from app.services.agent_tools import (
    _BuildError,
    _BuildErrorSummary,
    _KOTLIN_ERROR_RE,
    _JAVAC_ERROR_RE,
    _GENERAL_ERROR_RE,
    _BUILD_SUMMARY_RE,
    _parse_android_build_errors,
)

# ── 测试数据 ──

SAMPLE_KOTLIN_OUTPUT = """\
e: file:///app/src/Main.kt: (28, 13): Unresolved reference: Nonexistent
e: /app/src/App.kt: (8, 5): Unused parameter
w: /app/src/App.kt: (12, 9): Variable never used
"""

SAMPLE_JAVAC_OUTPUT = """\
/app/src/Main.java:8: error: ';' expected
/app/src/Utils.java:42: warning: [unchecked] unchecked cast
"""

SAMPLE_GENERAL_OUTPUT = """\
error: failed to read PNG signature
Error: Cannot find symbol
AAPT2 error: resource is not public.
"""

SAMPLE_MIXED_OUTPUT = (
    SAMPLE_KOTLIN_OUTPUT + SAMPLE_JAVAC_OUTPUT + SAMPLE_GENERAL_OUTPUT
)

SUMMARY_LINE = "27 errors, 5 warnings"

# ── 辅助函数 ──


def _freeze_summary(s: _BuildErrorSummary) -> tuple:
    """将 _BuildErrorSummary 转为可哈希的元组用于比较。"""
    return (
        tuple(
            (e.category, e.file, e.line, e.column, e.message) for e in s.errors
        ),
        tuple(
            (e.category, e.file, e.line, e.column, e.message) for e in s.warnings
        ),
        s.total_error_count,
        s.total_warning_count,
        s.unrecognized_lines,
    )


class Counts:
    passed = 0
    failed = 0

    def ok(self, name: str, detail: str = ""):
        self.passed += 1
        msg = f"  PASS  {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)

    def fail(self, name: str, detail: str):
        self.failed += 1
        print(f"  FAIL  {name} — {detail}")


cts = Counts()


# ════════════════════════════════════════════════════════════════
# Test 1: 相同输入 100 次调用产生一致输出
# ════════════════════════════════════════════════════════════════
print("\n=== Test 1: 幂等性 — 相同输入 100 次调用 ===")
results = [_parse_android_build_errors(SAMPLE_MIXED_OUTPUT) for _ in range(100)]
frozen_results = [_freeze_summary(r) for r in results]
first = frozen_results[0]
all_same = all(r == first for r in frozen_results)
if all_same:
    cts.ok("100 calls identical", f"{len(results)} calls, same input")
else:
    diffs = [i for i, r in enumerate(frozen_results) if r != first]
    cts.fail("100 calls NOT identical", f"differing indices: {diffs[:10]}")


# ════════════════════════════════════════════════════════════════
# Test 2: _BuildErrorSummary list 默认值每次新建实例
# ════════════════════════════════════════════════════════════════
print("\n=== Test 2: 默认值非共享引用 ===")
t1 = _BuildErrorSummary()
t2 = _BuildErrorSummary()
if t1.errors is not t2.errors:
    cts.ok("errors list not shared", "two instances have different list objects")
else:
    cts.fail("errors list IS shared", "t1.errors is t2.errors")
if t1.warnings is not t2.warnings:
    cts.ok("warnings list not shared", "two instances have different list objects")
else:
    cts.fail("warnings list IS shared", "t1.warnings is t2.warnings")


# ════════════════════════════════════════════════════════════════
# Test 3: 空/None/畸形输入连续调用不累积错误状态
# ════════════════════════════════════════════════════════════════
print("\n=== Test 3: 空/None/畸形输入连续调用 ===")

call3_results = [
    _parse_android_build_errors(""),
    _parse_android_build_errors(None),
    _parse_android_build_errors(123),
    _parse_android_build_errors([]),
    _parse_android_build_errors({}),
    _parse_android_build_errors("\n\n\n   \n\t\n"),
]

for i, r in enumerate(call3_results):
    empty = len(r.errors) == 0 and len(r.warnings) == 0
    if empty:
        cts.ok(f"call #{i} empty ({type(call3_results[i]).__name__})")
    else:
        cts.fail(
            f"call #{i} non-empty",
            f"input={repr(call3_results[i])}, errors={len(r.errors)}, warnings={len(r.warnings)}",
        )

# 连续调用的结果互斥
for i in range(len(call3_results)):
    for j in range(i + 1, len(call3_results)):
        if call3_results[i] is call3_results[j]:
            cts.fail(
                "shared instance across calls",
                f"call #{i} is call #{j}",
            )


# ════════════════════════════════════════════════════════════════
# Test 4: 模块级正则对象不被意外修改
# ════════════════════════════════════════════════════════════════
print("\n=== Test 4: 模块级正则对象不被意外修改 ===")

regexes_before = {
    "_KOTLIN_ERROR_RE": _KOTLIN_ERROR_RE.pattern,
    "_JAVAC_ERROR_RE": _JAVAC_ERROR_RE.pattern,
    "_GENERAL_ERROR_RE": _GENERAL_ERROR_RE.pattern,
    "_BUILD_SUMMARY_RE": _BUILD_SUMMARY_RE.pattern,
}

# 触发多次解析
for _ in range(50):
    _parse_android_build_errors(SAMPLE_MIXED_OUTPUT)
    _parse_android_build_errors("")
    _parse_android_build_errors(None)

regexes_after = {
    "_KOTLIN_ERROR_RE": _KOTLIN_ERROR_RE.pattern,
    "_JAVAC_ERROR_RE": _JAVAC_ERROR_RE.pattern,
    "_GENERAL_ERROR_RE": _GENERAL_ERROR_RE.pattern,
    "_BUILD_SUMMARY_RE": _BUILD_SUMMARY_RE.pattern,
}

if regexes_before == regexes_after:
    cts.ok("regex patterns unchanged after 100+ parse calls")
else:
    for name in regexes_before:
        if regexes_before[name] != regexes_after[name]:
            cts.fail(f"{name} pattern changed", f"before={regexes_before[name][:50]} after={regexes_after[name][:50]}")


# ════════════════════════════════════════════════════════════════
# Test 5: 两次独立解析结果不互相影响
# ════════════════════════════════════════════════════════════════
print("\n=== Test 5: 独立解析结果不互相影响 ===")
r1 = _parse_android_build_errors(SAMPLE_KOTLIN_OUTPUT)
r2 = _parse_android_build_errors(SAMPLE_JAVAC_OUTPUT)

# 修改 r1 的副本不影响 r2
# 注意: _BuildErrorSummary 不是 frozen
orig_r1_len = len(r1.errors)
r1_errors_copy = list(r1.errors)
r1_warnings_copy = list(r1.warnings)

# 修改一个创建后不影响原实例的属性
r1.total_error_count = 999
r1.unrecognized_lines = 888

if r2.total_error_count != 999:
    cts.ok(
        "r2.total_error_count unchanged after r1 mutation",
        f"r2.total={r2.total_error_count}",
    )
else:
    cts.fail("r2 contaminated by r1 mutation", "r2.total_error_count == 999")

if r2.unrecognized_lines != 888:
    cts.ok("r2.unrecognized_lines unchanged after r1 mutation")
else:
    cts.fail("r2.unrecognized_lines contaminated")


# ════════════════════════════════════════════════════════════════
# Test 6: frozen=True dataclass 实例不可修改
# ════════════════════════════════════════════════════════════════
print("\n=== Test 6: frozen=True _BuildError 不可修改 ===")
err = _BuildError(category="error", file="Test.kt", line=1, column=2, message="test")

freeze_ok = True
try:
    err.category = "warning"
except dataclasses.FrozenInstanceError:
    pass
except AttributeError:
    pass
else:
    freeze_ok = False

if (
    freeze_ok
    and err.category == "error"
    and err.file == "Test.kt"
    and err.line == 1
    and err.column == 2
    and err.message == "test"
):
    cts.ok(
        "FrozenInstanceError raised on attribute set",
        "category still 'error', value unchanged",
    )
else:
    cts.fail(
        "FrozenInstancedError NOT raised",
        "category changed or wrong initial state",
    )


# ════════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"Total: {cts.passed + cts.failed} tests")
if cts.failed == 0:
    print(f"Result: ALL PASS ({cts.passed}/{cts.passed + cts.failed})")
else:
    print(f"Result: {cts.failed} FAILED, {cts.passed} PASSED")
    sys.exit(1)
