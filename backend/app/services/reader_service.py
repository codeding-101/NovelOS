"""读者数据回环：把平台后台的章节数据收回来，验证我们的判定对不对。

为什么要有这一层：文本层的阈值全是我按经验定的。作者说「读者是最好的测试人员」，
那就得把读者的实际表现贴回来，才能回答两个问题：
1. 我们判定偏弱的章，读者是否真的掉队了？（判定有没有效）
2. 我们没报警但读者明显掉队的章，是哪一章？（我们漏了什么）

导入刻意做得宽容：直接粘贴从平台后台复制的表格就行，制表符、逗号、多空格都能分列，
带百分号、带千分位、带「第N章」这样的前缀都能认。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Chapter, ClaimReport, ContinuityReport, Novel, ReaderMetric
from app.services import style_service

#: 导入时表头行会被跳过（含这些词就当作表头）
_HEADER_HINTS = ("章", "阅读", "完读", "追读", "人数", "收益", "评论")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _to_number(text: str) -> float | None:
    match = _NUMBER_RE.search((text or "").replace(",", ""))
    if not match:
        return None
    return float(match.group(0))


def _to_rate(text: str) -> float | None:
    """把「23.5%」「0.235」「23.5」都归一到 0~1 的比例。"""
    value = _to_number(text)
    if value is None:
        return None
    if "%" in (text or ""):
        return round(value / 100, 6)
    if value > 1.0:
        return round(value / 100, 6)
    return round(value, 6)


def _split_row(line: str) -> list[str]:
    if "\t" in line:
        parts = line.split("\t")
    elif "," in line:
        parts = line.split(",")
    else:
        parts = re.split(r"\s{2,}", line)
    return [part.strip() for part in parts if part.strip() != ""]


def parse_reader_rows(text: str) -> list[dict[str, Any]]:
    """把粘贴的表格解析成行。返回 {chapter_number, reads, completion_rate, ...}。"""
    rows: list[dict[str, Any]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = _split_row(line)
        if not parts:
            continue
        chapter_match = re.search(r"第?\s*(\d+)\s*章?", parts[0])
        if not chapter_match:
            continue
        if all(hint in line for hint in ("章", "阅读")) and _to_number(parts[1] if len(parts) > 1 else "") is None:
            continue  # 表头
        chapter_number = int(chapter_match.group(1))
        numbers = parts[1:]
        row: dict[str, Any] = {
            "chapter_number": chapter_number,
            "raw": {"line": line},
        }
        if numbers:
            reads = _to_number(numbers[0])
            row["reads"] = int(reads) if reads is not None else 0
            row["raw"]["reads"] = numbers[0]
        if len(numbers) > 1:
            row["completion_rate"] = _to_rate(numbers[1]) or 0.0
            row["raw"]["completion_rate"] = numbers[1]
        if len(numbers) > 2:
            row["retention_rate"] = _to_rate(numbers[2])
            row["raw"]["retention_rate"] = numbers[2]
        if len(numbers) > 3:
            row["revenue"] = _to_number(numbers[3])
            row["raw"]["revenue"] = numbers[3]
        if len(numbers) > 4:
            comments = _to_number(numbers[4])
            row["comments"] = int(comments) if comments is not None else 0
            row["raw"]["comments"] = numbers[4]
        rows.append(row)
    return rows


def import_rows(
    session: Session, novel: Novel, text: str, *, note: str = ""
) -> dict[str, Any]:
    """导入（或覆盖）章节数据，返回导入结果。"""
    rows = parse_reader_rows(text)
    if not rows:
        return {"imported": 0, "rows": [], "message": "没解析出任何一行：每行至少要有「章号 + 阅读人数」"}

    existing = {
        item.chapter_number: item
        for item in session.scalars(
            select(ReaderMetric).where(ReaderMetric.novel_id == novel.id)
        )
    }
    imported: list[dict[str, Any]] = []
    for row in rows:
        current = existing.get(row["chapter_number"])
        if current is None:
            current = ReaderMetric(novel_id=novel.id, chapter_number=row["chapter_number"], raw={})
            session.add(current)
        current.reads = int(row.get("reads") or 0)
        current.completion_rate = float(row.get("completion_rate") or 0.0)
        current.retention_rate = row.get("retention_rate")
        current.revenue = row.get("revenue")
        current.comments = int(row.get("comments") or 0)
        current.raw = row["raw"]
        if note:
            current.note = note
        imported.append({"chapter_number": current.chapter_number, "reads": current.reads,
                         "completion_rate": current.completion_rate})
    session.flush()
    return {
        "imported": len(imported),
        "rows": imported,
        "message": f"导入 {len(imported)} 章的数据（同章号会覆盖）",
    }


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _issue_counts(session: Session, novel: Novel) -> dict[int, tuple[int, int]]:
    """每章的（审校错误数，断言冲突数），与结构视图同源。"""
    counts: dict[int, tuple[int, int]] = {}
    for report in session.scalars(
        select(ContinuityReport)
        .where(ContinuityReport.novel_id == novel.id)
        .order_by(ContinuityReport.created_at)
    ):
        if report.chapter_number is None:
            continue
        previous = counts.get(report.chapter_number, (0, 0))[1]
        counts[report.chapter_number] = (len(report.errors or []), previous)
    for report in session.scalars(
        select(ClaimReport).where(ClaimReport.novel_id == novel.id)
    ):
        if report.chapter_number is None:
            continue
        errors = counts.get(report.chapter_number, (0, 0))[0]
        counts[report.chapter_number] = (errors, report.conflict_count or 0)
    return counts


def reader_analysis(session: Session, novel: Novel) -> dict[str, Any]:
    """把读者数据与规则判定并排放在一起，给出一致性结论与校准建议。"""
    metrics = {
        item.chapter_number: item
        for item in session.scalars(
            select(ReaderMetric).where(ReaderMetric.novel_id == novel.id)
        )
    }
    chapters = list(
        session.scalars(
            select(Chapter)
            .where(Chapter.novel_id == novel.id)
            .order_by(Chapter.chapter_number)
        )
    )
    # 弱章判据与结构视图保持一致：节奏三项 + 审校错误 + 断言冲突。
    # 只算前三项时，像第 15 章那种「钩子和推进都够、但有 7 条审校错误」的章会被算成「漏报」——
    # 那是判定口径不一致，不是我们真漏了。
    issues_by_chapter = _issue_counts(session, novel)
    rows: list[dict[str, Any]] = []
    for chapter in chapters:
        metric = metrics.get(chapter.chapter_number)
        text = chapter.content or ""
        measurements = style_service.measure(text) if text else None
        errors, conflicts = issues_by_chapter.get(chapter.chapter_number, (0, 0))
        weak_reasons: list[str] = []
        if measurements is not None:
            if measurements.hook_score < 0.3:
                weak_reasons.append("章末钩子偏弱")
            if measurements.advancement_per_1k < 4.0:
                weak_reasons.append("推进信号偏少")
            if measurements.filler_paragraph_ratio > 0.35:
                weak_reasons.append("注水段落偏多")
        if errors:
            weak_reasons.append(f"审校错误 {errors} 条")
        if conflicts:
            weak_reasons.append(f"设定断言冲突 {conflicts} 条")
        rows.append(
            {
                "chapter_number": chapter.chapter_number,
                "title": chapter.title or "",
                "word_count": chapter.word_count or 0,
                "reads": metric.reads if metric else None,
                "completion_rate": metric.completion_rate if metric else None,
                "retention_rate": metric.retention_rate if metric else None,
                "hook_score": round(measurements.hook_score, 3) if measurements else 0.0,
                "advancement_per_1k": round(measurements.advancement_per_1k, 2) if measurements else 0.0,
                "filler_paragraph_ratio": round(measurements.filler_paragraph_ratio, 3)
                if measurements
                else 0.0,
                "weak": bool(weak_reasons),
                "weak_reasons": weak_reasons,
            }
        )

    with_data = [row for row in rows if row["completion_rate"] is not None]
    if not with_data:
        return {
            "novel_id": novel.id,
            "title": novel.title,
            "chapters": rows,
            "coverage": {"with_data": 0, "total": len(rows)},
            "verdict": "还没有导入读者数据：结构面板与发布检查的阈值目前都是经验值，导入后才能校准",
            "suggestions": [
                "从平台后台复制「章号 / 阅读人数 / 完读率」这几列贴进来即可，制表符或逗号分隔都认",
            ],
            "missed": [],
            "false_alarms": [],
            "drop_chapters": [],
        }

    book_completion = _mean([row["completion_rate"] for row in with_data])
    weak_rows = [row for row in with_data if row["weak"]]
    ok_rows = [row for row in with_data if not row["weak"]]
    weak_mean = _mean([row["completion_rate"] for row in weak_rows])
    ok_mean = _mean([row["completion_rate"] for row in ok_rows])

    # 判定是否有效：弱章的完读率明显低于其余章，说明我们抓的东西跟读者流失相关
    weak_flagged = bool(weak_rows and ok_rows)
    verdict_signal = "数据不足"
    if weak_flagged:
        gap = round((ok_mean - weak_mean) * 100, 1)
        if gap >= 3:
            verdict_signal = f"相符：报出来的弱章平均完读率 {round(weak_mean * 100, 1)}%，其余章 {round(ok_mean * 100, 1)}%，低 {gap} 个百分点"
        elif gap <= -3:
            verdict_signal = f"可能过严：报出来的弱章完读率反而更高（{round(weak_mean * 100, 1)}% vs {round(ok_mean * 100, 1)}%）"
        else:
            verdict_signal = f"不明显：弱章与其余章的完读率只差 {gap} 个百分点，判定可能没抓到读者真正在意的东西"

    # 漏报：没报警但完读率明显低于全书平均
    missed = [
        {
            "chapter_number": row["chapter_number"],
            "title": row["title"],
            "completion_rate": row["completion_rate"],
            "gap_vs_book": round((row["completion_rate"] - book_completion) * 100, 1),
        }
        for row in ok_rows
        if row["completion_rate"] < book_completion - 0.05
    ]
    missed.sort(key=lambda item: item["gap_vs_book"])

    # 误报：报警了但读者没跑
    false_alarms = [
        {
            "chapter_number": row["chapter_number"],
            "title": row["title"],
            "completion_rate": row["completion_rate"],
            "reasons": row["weak_reasons"],
        }
        for row in weak_rows
        if row["completion_rate"] > book_completion + 0.03
    ]

    # 读者在哪里掉的：阅读人数相对上一章下降最多的几章
    drops: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for row in rows:
        if row["reads"] is None:
            continue
        if previous is not None and previous["reads"]:
            change = (row["reads"] - previous["reads"]) / previous["reads"]
            if change <= -0.08:
                drops.append(
                    {
                        "chapter_number": row["chapter_number"],
                        "title": row["title"],
                        "reads": row["reads"],
                        "from_chapter": previous["chapter_number"],
                        "change": round(change, 4),
                    }
                )
        previous = row
    drops.sort(key=lambda item: item["change"])

    suggestions: list[str] = []
    if len(missed) > len(false_alarms):
        suggestions.append(
            f"漏报比误报多（{len(missed)} vs {len(false_alarms)}）：我们的指标没抓住读者在意的东西，"
            "先看下面「我们没报警但读者走了」的那几章，找出共同点再补指标"
        )
    elif len(false_alarms) > len(missed):
        suggestions.append(
            f"误报比漏报多（{len(false_alarms)} vs {len(missed)}）：阈值偏严，"
            "可以按本书数据放宽（钩子 0.30、推进 4.0/千字、注水 35% 都写在结构面板上）"
        )
    if drops:
        suggestions.append(
            f"阅读人数掉得最狠的是第 {drops[0]['chapter_number']} 章（比上一章少 "
            f"{abs(round(drops[0]['change'] * 100))}%）：先看这一章的开头与结尾，读者多半是在那里退出的"
        )
    if not suggestions:
        suggestions.append("判定与读者数据没有明显矛盾：继续用现在的阈值，攒更多章再校准")

    return {
        "novel_id": novel.id,
        "title": novel.title,
        "chapters": rows,
        "coverage": {"with_data": len(with_data), "total": len(rows)},
        "book_completion_rate": book_completion,
        "weak_mean_completion": weak_mean,
        "ok_mean_completion": ok_mean,
        "verdict": verdict_signal,
        "missed": missed[:10],
        "false_alarms": false_alarms[:10],
        "drop_chapters": drops[:10],
        "suggestions": suggestions,
    }
