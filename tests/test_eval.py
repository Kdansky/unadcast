import json

import pytest

from unadcast import evaluate, runner

from tests.conftest import ads


def test_score_counts_seconds():
    plan = {"duration": 100.0, "cuts": [{"start": 10, "end": 30}], "marks": [{"start": 50, "end": 60}, {"start": 25, "end": 35}]}
    s = evaluate.score(plan, [[10, 20], [50, 70], [90, None]])
    assert s["ad"] == pytest.approx(40)
    assert s["cut_ad"] == pytest.approx(10)
    assert s["mark_ad"] == pytest.approx(10)
    assert s["missed"] == pytest.approx(20)
    # Marks under a cut count as cut; the mark from 30 to 35 is show.
    assert s["cut_show"] == pytest.approx(10)
    assert s["mark_show"] == pytest.approx(5)


def test_evaluate_labelled_episodes(make_ctx, feed_url, tmp_path):
    ctx = make_ctx(feeds=[{"slug": "show", "url": feed_url}])
    runner.run(ctx, log=lambda m: None)
    labels = tmp_path / "labels" / "show"
    labels.mkdir(parents=True)
    by = {e.title: e for e in ctx.pending("show")}
    for title in ("ep1", "ep2"):
        (labels / f"{by[title].id}.json").write_text(json.dumps({"title": title, "ads": [list(a) for a in ads(title)]}))

    rows = evaluate.evaluate(ctx)
    assert [r["title"] for r in rows] == ["ep1", "ep2"]
    ep1 = rows[0]["score"]
    assert ep1["cut_ad"] / ep1["ad"] > 0.9 and ep1["cut_show"] < 3

    # Re-fusing with only the loudness detector: nothing corroborates, so everything is marked and nothing cut.
    alone = evaluate.evaluate(ctx, only={"loudness"})
    assert all(r["score"]["cut_ad"] == 0 and r["score"]["mark_ad"] > 0 for r in alone)


def test_report_has_totals():
    rows = [{"feed": "f", "title": "a", "score": dict(ad=100, cut_ad=80, mark_ad=10, missed=10, cut_show=2, mark_show=5)}] * 2
    text = evaluate.report(rows)
    assert "total" in text and "80%" in text
