import sys
sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.services.llm.compression_result import Lossiness, requires_ccr, unchanged
from app.services.llm.content_router import compress_one_result


def test_lossless_does_not_require_ccr():
    r = unchanged("hello", tokens=1)
    assert not requires_ccr(r)
    assert r.lossiness == Lossiness.NONE


def test_tail_requires_ccr():
    from app.services.llm.compression_result import CompressionResult
    r = CompressionResult(
        content="x", changed=True, lossiness=Lossiness.TAIL, recoverable=True,
        strategy="list_head_tail", original_tokens=100, final_tokens=50,
    )
    assert requires_ccr(r)


def test_lossless_changed_no_ccr():
    from app.services.llm.compression_result import CompressionResult
    r = CompressionResult(
        content="{}", changed=True, lossiness=Lossiness.LOSSLESS, recoverable=False,
        strategy="json_lossless", original_tokens=100, final_tokens=80,
    )
    assert not requires_ccr(r)


def test_list_files_excluded_no_change():
    big = "\n".join(f"f{i}.txt" for i in range(500))
    r = compress_one_result(big, tool_name="list_files", budget_tokens=10, model_name="")
    assert not r.changed
    assert not requires_ccr(r)
