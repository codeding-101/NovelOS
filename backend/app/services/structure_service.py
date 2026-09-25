"""结构视图：把全书每章的节奏与结构事件排成一条线，找出「连续弱区」。

为什么单独做这一层：文本层的规则（套话、用词、格式）已经能压住「文面」问题，
但平台点名的「结构失常」「剧情逻辑混乱」「大段内容未能推动情节发展」，
以及读者会在哪一段掉队，靠单章指标看不出来 —— 要跨章连起来看。

这里不发明新的数据结构，只把已有的东西按章对齐：每章的钩子分、推进密度、注水比例、
冲突密度、字数，加上结构事件（伏笔首现/推进、承诺到期、审校错误、断言冲突），
再把连续不达标的区间收成「弱区」，把每 5 章的节奏窗口标出来。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Chapter, ClaimReport, Commitment, ContinuityReport, Foreshadowing, Novel
from app.services import style_service

#: 节奏窗口：网文常见的「每 3~5 章一个小高潮」
PACE_WINDOW = 5
#: 单章达标的经验下限（与发布前检查同源，先把阈值摆在明面上，之后按平台数据校准）
HOOK_FLOOR = 0.3
ADVANCEMENT_FLOOR = 4.0
FILLER_CEILING = 0.35
#: 张弛度：平台课《拒绝流水账》要求「张弛有度」——一直紧读者累，一直松读者走。
#: 用窗口内的冲突信号密度做代理：高于上限=一直紧，低于下限=一直松。
CONFLICT_CEILING = 32.0
CONFLICT_FLOOR = 8.0

FLOORS = {
    "hook_floor": HOOK_FLOOR,
    "advancement_floor": ADVANCEMENT_FLOOR,
    "filler_ceiling": FILLER_CEILING,
    "conflict_ceiling": CONFLICT_CEILING,
    "conflict_floor": CONFLICT_FLOOR,
    "pace_window": PACE_WINDOW,
}


def _chapter_signals(session: Session, novel: Novel) -> list[dict[str, Any]]:
    chapters = list(
        session.scalars(
            select(Chapter)
            .where(Chapter.novel_id == novel.id, Chapter.content.is_not(None))
            .order_by(Chapter.chapter_number)
        )
    )
    reports: dict[int, ContinuityReport] = {}
    for report in session.scalars(
        select(ContinuityReport)
        .where(ContinuityReport.novel_id == novel.id)
        .order_by(ContinuityReport.created_at)
    ):
        if report.chapter_number is not None:
            reports[report.chapter_number] = report  # 后写的覆盖先写的
    conflicts: dict[int, int] = {}
    for report in session.scalars(
        select(ClaimReport).where(ClaimReport.novel_id == novel.id)
    ):
        if report.chapter_number is None:
            continue
        conflicts[report.chapter_number] = conflicts.get(report.chapter_number, 0) + (
            report.conflict_count or 0
        )

    rows: list[dict[str, Any]] = []
    for chapter in chapters:
        metrics = style_service.measure(chapter.content or "")
        continuity = reports.get(chapter.chapter_number)
        row = {
            "chapter_id": chapter.id,
            "chapter_number": chapter.chapter_number,
            "title": chapter.title or "",
            "word_count": chapter.word_count or metrics.total_chars,
            "hook_score": metrics.hook_score,
            "advancement_per_1k": metrics.advancement_per_1k,
            "filler_paragraph_ratio": metrics.filler_paragraph_ratio,
            "conflict_per_1k": metrics.conflict_per_1k,
            "dialogue_ratio": metrics.dialogue_ratio,
            "continuity_errors": len(continuity.errors or []) if continuity else 0,
            "continuity_warnings": len(continuity.warnings or []) if continuity else 0,
            "claim_conflicts": conflicts.get(chapter.chapter_number, 0),
            "foreshadowing_opened": [],
            "foreshadowing_advanced": [],
            "commitments_due": [],
        }
        rows.append(row)
    return rows


def _attach_events(session: Session, novel: Novel, rows: list[dict[str, Any]]) -> None:
    by_number = {row["chapter_number"]: row for row in rows}

    for item in session.scalars(
        select(Foreshadowing).where(Foreshadowing.novel_id == novel.id)
    ):
        if item.first_chapter in by_number:
            by_number[item.first_chapter]["foreshadowing_opened"].append(item.name)
        if (
            item.last_reinforced_chapter in by_number
            and item.last_reinforced_chapter != item.first_chapter
        ):
            by_number[item.last_reinforced_chapter]["foreshadowing_advanced"].append(item.name)

    for commitment in session.scalars(
        select(Commitment).where(Commitment.novel_id == novel.id)
    ):
        due = commitment.fulfilled_chapter or commitment.source_chapter
        if due in by_number:
            by_number[due]["commitments_due"].append(commitment.what)


def _chapter_verdicts(row: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if row["hook_score"] < HOOK_FLOOR:
        reasons.append(f"章末钩子偏弱（{round(row['hook_score'], 2)} < {HOOK_FLOOR}）")
    if row["advancement_per_1k"] < ADVANCEMENT_FLOOR:
        reasons.append(
            f"推进信号偏少（每千字 {round(row['advancement_per_1k'], 1)} < {ADVANCEMENT_FLOOR}）"
        )
    if row["filler_paragraph_ratio"] > FILLER_CEILING:
        reasons.append(f"注水段落偏多（{round(row['filler_paragraph_ratio'] * 100)}%）")
    if row["continuity_errors"]:
        reasons.append(f"审校错误 {row['continuity_errors']} 条")
    if row["claim_conflicts"]:
        reasons.append(f"设定断言冲突 {row['claim_conflicts']} 条")
    return reasons


def _weak_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """连续不达标的区间：单章弱可以忍，连着弱是读者会走的地方。"""
    runs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in rows:
        reasons = _chapter_verdicts(row)
        if reasons:
            if current is None:
                current = {
                    "start_chapter": row["chapter_number"],
                    "end_chapter": row["chapter_number"],
                    "reasons": set(),
                }
            current["end_chapter"] = row["chapter_number"]
            current["reasons"].update(reason.split("（")[0] for reason in reasons)
        else:
            if current is not None:
                runs.append(current)
                current = None
    if current is not None:
        runs.append(current)
    weak = [
        {
            "start_chapter": run["start_chapter"],
            "end_chapter": run["end_chapter"],
            "length": run["end_chapter"] - run["start_chapter"] + 1,
            "reasons": sorted(run["reasons"]),
        }
        for run in runs
    ]
    weak.sort(key=lambda item: (-item["length"], item["start_chapter"]))
    return weak


def _pace_windows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每 5 章一个窗口，看这一段整体有没有「小高潮」。

    两个判据：绝对下限（低于 hook_floor 就是缺钩子），以及相对全书平均的低谷
    （读者感觉到的「这一段变闷了」通常是对比出来的，不是绝对水平）。
    """
    windows: list[dict[str, Any]] = []
    book_avg = sum(row["hook_score"] for row in rows) / len(rows) if rows else 0.0
    for start in range(0, len(rows), PACE_WINDOW):
        chunk = rows[start : start + PACE_WINDOW]
        if not chunk:
            continue
        hook = sum(row["hook_score"] for row in chunk) / len(chunk)
        advancement = sum(row["advancement_per_1k"] for row in chunk) / len(chunk)
        conflict = sum(row["conflict_per_1k"] for row in chunk) / len(chunk)
        words = sum(row["word_count"] for row in chunk)
        peak = max(chunk, key=lambda row: row["hook_score"])
        if hook < HOOK_FLOOR:
            verdict = "这一段整体缺钩子：连续多章结尾不留悬念，读者容易在这里断"
        elif book_avg and hook < book_avg * 0.85:
            verdict = (
                f"这一段是全书低谷：平均钩子 {round(hook, 2)}，比全书平均"
                f"（{round(book_avg, 2)}）低 {round((1 - hook / book_avg) * 100)}%"
            )
        elif conflict > CONFLICT_CEILING:
            verdict = (
                f"这一段一直紧：平均冲突信号 {round(conflict, 1)}/千字，"
                "中间没有喘息段，读者会累（平台课：节奏要张弛有度）"
            )
        elif conflict < CONFLICT_FLOOR:
            verdict = (
                f"这一段一直松：平均冲突信号只有 {round(conflict, 1)}/千字，"
                "缺少张力，读者容易放下（平台课：节奏要张弛有度）"
            )
        else:
            verdict = "这一段有起伏"
        windows.append(
            {
                "start_chapter": chunk[0]["chapter_number"],
                "end_chapter": chunk[-1]["chapter_number"],
                "avg_hook": round(hook, 3),
                "avg_advancement": round(advancement, 2),
                "avg_conflict": round(conflict, 2),
                "book_avg_hook": round(book_avg, 3),
                "words": words,
                "peak_chapter": peak["chapter_number"],
                "verdict": verdict,
            }
        )
    return windows


def structure_view(session: Session, novel: Novel) -> dict[str, Any]:
    """全书结构视图：逐章信号 + 弱区 + 节奏窗口。"""
    rows = _chapter_signals(session, novel)
    _attach_events(session, novel, rows)
    for row in rows:
        row["verdicts"] = _chapter_verdicts(row)
    weak = _weak_runs(rows)
    windows = _pace_windows(rows)
    total_words = sum(row["word_count"] for row in rows)
    return {
        "novel_id": novel.id,
        "title": novel.title,
        "chapter_count": len(rows),
        "word_count": novel.word_count or total_words,
        "target_word_count": novel.target_word_count,
        "floors": FLOORS,
        "chapters": rows,
        "weak_runs": weak,
        "pace_windows": windows,
        "summary": {
            "weak_chapters": sum(1 for row in rows if row["verdicts"]),
            "weakest_run": weak[0] if weak else None,
            "weakest_window": min(windows, key=lambda item: item["avg_hook"]) if windows else None,
        },
    }
