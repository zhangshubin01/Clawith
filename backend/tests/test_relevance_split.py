"""relevance_split 单元测试。"""

from app.services.llm.relevance_split import BM25Scorer, build_relevance_query, plan_relevance_split, segment


def test_segment_roundtrip():
    original = "a\nb\n\nc\n" + "d\n" * 20
    assert "".join(segment(original, window=3)) == original


def test_bm25_scores_exact_identifier_higher():
    scores = BM25Scorer().score_batch(["alpha beta", "needle_401 failure"], "needle_401")
    assert scores[1].score > scores[0].score


def test_plan_relevance_split_keeps_matching_run():
    content = "\n".join(
        [f"src/noise.py:{i}: boring line" for i in range(30)]
        + ["src/target.py:40: needle_401 failure here"]
        + [f"src/noise.py:{i}: boring line" for i in range(31, 60)]
    )
    query = build_relevance_query("why needle_401 failed", "search_clawhub", "query=needle_401")
    runs = plan_relevance_split(content, query, BM25Scorer(), threshold=0.25)
    kept = "".join(chunk for keep, chunk in runs if keep)
    dropped = "".join(chunk for keep, chunk in runs if not keep)
    assert "needle_401 failure" in kept
    assert "boring line" in dropped
