"""全局不变量：只有把整本书放在一起看才能发现的问题。

单章审校回答的是「这一章和 Canon 有没有冲突」；这里回答的是**整本书层面**的问题：

- 故事时间必须单调（第 20 章不能比第 15 章更早）；
- 修为/境界不能倒退（除非正文交代了散功之类的变故）；
- 同一时点不能同时持有两把不同的武器；
- 同一个人不能在同一天出现在两地；
- 知情范围只能扩大（除非有「失忆 / 隐瞒」情节）；
- 已到期的承诺必须兑现（交给承诺账本判定）。

这些都是确定性规则，因此可以进测试、可以在每次改动后重跑。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CanonFact, CanonStatus, Chapter, Character, InvariantReport, Novel
from app.services import commitment_service
from app.timeutil import cn_number_to_int, format_chapter_ref, story_time_to_day

#: 境界顺序与层数换算（炼气九层 = 1*10+9）
REALM_BASE = {"炼气": 1, "筑基": 2, "金丹": 3, "元婴": 4, "化神": 5, "炼体": 1}
REALM_STAGE_BONUS = {"初期": 1, "中期": 2, "后期": 3, "巅峰": 4, "圆满": 4, "大圆满": 5}
LAYER_WORDS = ("一层", "二层", "三层", "四层", "五层", "六层", "七层", "八层", "九层")
#: 这些谓词属于「同一时点只能有一个取值」的排他类型
EXCLUSIVE_PREDICATES = ("佩剑", "武器", "本命灵剑", "兵器", "法宝")
DEATH_TOKENS = ("死亡", "已死", "身亡", "战死", "殒命", "气绝")


def realm_level(value: str | None) -> int | None:
    """把「炼气九层」「筑基中期」这类写法转成可比较的等级；解析不出来返回 None。"""
    text = (value or "").strip()
    if not text:
        return None
    for name, base in REALM_BASE.items():
        if not text.startswith(name):
            continue
        level = base * 100
        for index, layer in enumerate(LAYER_WORDS, start=1):
            if layer in text:
                return level + index
        for stage, bonus in REALM_STAGE_BONUS.items():
            if stage in text:
                return level + bonus
        return level
    return None


def _issue(
    code: str,
    level: str,
    message: str,
    *,
    subject: str = "",
    evidence: list[dict[str, Any]] | None = None,
    suggestion: str = "",
) -> dict[str, Any]:
    return {
        "code": code,
        "level": level,
        "subject": subject,
        "message": message,
        "evidence": evidence or [],
        "suggestion": suggestion,
    }


def _chapters(session: Session, novel_id: str) -> list[Chapter]:
    return list(
        session.scalars(
            select(Chapter).where(Chapter.novel_id == novel_id).order_by(Chapter.chapter_number)
        )
    )


def _canon_facts(session: Session, novel_id: str) -> list[CanonFact]:
    return list(
        session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel_id,
                CanonFact.status.in_((CanonStatus.CANON, CanonStatus.SUPERSEDED)),
            )
        )
    )


# --------------------------------------------------------------------------- 规则
def check_time_monotonic(chapters: list[Chapter]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    previous: Chapter | None = None
    previous_day: int | None = None
    for chapter in chapters:
        day = story_time_to_day(chapter.story_time)
        if day is None:
            continue
        if previous is not None and previous_day is not None and day < previous_day:
            issues.append(
                _issue(
                    "TIME_INVERSION",
                    "error",
                    f"故事时间回退：{format_chapter_ref(chapter.chapter_number)}（{chapter.story_time}）"
                    f"早于 {format_chapter_ref(previous.chapter_number)}（{previous.story_time}）",
                    subject="故事时间",
                    evidence=[
                        {
                            "source_chapter": format_chapter_ref(previous.chapter_number),
                            "quote": previous.story_time,
                            "detail": f"该章故事时间为 {previous.story_time}",
                        },
                        {
                            "source_chapter": format_chapter_ref(chapter.chapter_number),
                            "quote": chapter.story_time,
                            "detail": f"该章故事时间为 {chapter.story_time}",
                        },
                    ],
                    suggestion="确认是否有意倒叙；如是笔误，请改回顺序正确的时间",
                )
            )
        previous, previous_day = chapter, day
    return issues


def check_realm_regression(facts: list[CanonFact]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    by_subject: dict[str, list[CanonFact]] = {}
    for fact in facts:
        if fact.predicate in ("修为", "境界"):
            by_subject.setdefault(fact.subject, []).append(fact)
    for subject, items in by_subject.items():
        ordered = sorted(items, key=lambda fact: (fact.valid_from_chapter or 0, fact.created_at))
        best: tuple[int, CanonFact] | None = None
        for fact in ordered:
            level = realm_level(fact.object)
            if level is None:
                continue
            if best is not None and level < best[0]:
                issues.append(
                    _issue(
                        "REALM_REGRESSION",
                        "error",
                        f"{subject}的修为从「{best[1].object}」退回「{fact.object}」",
                        subject=subject,
                        evidence=[
                            {
                                "source_chapter": format_chapter_ref(best[1].source_chapter),
                                "quote": best[1].object,
                                "detail": f"该章起修为为 {best[1].object}",
                            },
                            {
                                "source_chapter": format_chapter_ref(fact.source_chapter),
                                "quote": fact.object,
                                "detail": f"该章写成了 {fact.object}",
                            },
                        ],
                        suggestion="要么交代掉境/受伤的原因，要么把修为改回不倒退的写法",
                    )
                )
            if best is None or level >= best[0]:
                best = (level, fact)
    return issues


def check_exclusive_possession(facts: list[CanonFact]) -> list[dict[str, Any]]:
    """同一时点不能同时持有两件不同的排他物（佩剑/武器/法宝）。"""
    issues: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[CanonFact]] = {}
    for fact in facts:
        if fact.predicate in EXCLUSIVE_PREDICATES:
            grouped.setdefault((fact.subject, fact.predicate), []).append(fact)
    for (subject, predicate), items in grouped.items():
        windows: list[tuple[int, int, CanonFact]] = []
        for fact in items:
            start = fact.valid_from_chapter or 1
            end = fact.valid_until_chapter or 10_000
            windows.append((start, end, fact))
        windows.sort()
        for index, (start, end, fact) in enumerate(windows):
            for other_start, other_end, other in windows[index + 1 :]:
                if other_start >= end:
                    break
                if other.object == fact.object:
                    continue
                issues.append(
                    _issue(
                        "EXCLUSIVE_CONFLICT",
                        "error",
                        f"{subject}在{format_chapter_ref(max(start, other_start))}前后同时存在"
                        f"两种{predicate}：「{fact.object}」与「{other.object}」",
                        subject=subject,
                        evidence=[
                            {
                                "source_chapter": format_chapter_ref(fact.source_chapter),
                                "quote": fact.object,
                                "detail": f"第{start}章起生效，第{fact.valid_until_chapter or '至今'}章失效",
                            },
                            {
                                "source_chapter": format_chapter_ref(other.source_chapter),
                                "quote": other.object,
                                "detail": f"第{other_start}章起生效，第{other.valid_until_chapter or '至今'}章失效",
                            },
                        ],
                        suggestion=(
                            f"同一时点只能有一件{predicate}；请确认其中一条的生效/失效章号，"
                            "或在正文里交代替换过程"
                        ),
                    )
                )
    return issues


def check_knowledge_shrink(facts: list[CanonFact]) -> list[dict[str, Any]]:
    """同一设定被新事实取代时，知情范围不应缩小（除非有失忆/隐瞒情节）。"""
    issues: list[dict[str, Any]] = []
    by_id = {fact.id: fact for fact in facts}
    for fact in facts:
        if not fact.superseded_by:
            continue
        successor = by_id.get(fact.superseded_by)
        if successor is None:
            continue
        lost = set(fact.known_by or []) - set(successor.known_by or [])
        if not lost:
            continue
        issues.append(
            _issue(
                "KNOWLEDGE_SHRINK",
                "warning",
                f"「{fact.subject}的{fact.predicate}」更新后，{'、'.join(sorted(lost))} 不再知情",
                subject=fact.subject,
                evidence=[
                    {
                        "source_chapter": format_chapter_ref(fact.source_chapter),
                        "quote": fact.object,
                        "detail": f"旧事实：{fact.object}，知情者 {'、'.join(fact.known_by or []) or '未记录'}",
                    },
                    {
                        "source_chapter": format_chapter_ref(successor.source_chapter),
                        "quote": successor.object,
                        "detail": f"新事实：{successor.object}，知情者 {'、'.join(successor.known_by or []) or '未记录'}",
                    },
                ],
                suggestion="知情范围通常只增不减；如是失忆/隐瞒情节，请在人物状态里写明",
            )
        )
    return issues


def check_location_continuity(session: Session, novel: Novel) -> list[dict[str, Any]]:
    """同一天出现在两地：相邻两次位置记录若同一天且位置不同，需要交代移动。"""
    issues: list[dict[str, Any]] = []
    chapters = _chapters(session, novel.id)
    by_id = {chapter.id: chapter for chapter in chapters}
    by_number = {chapter.chapter_number: chapter for chapter in chapters}
    for character in session.scalars(
        select(Character).where(Character.novel_id == novel.id)
    ):
        records: list[tuple[int, int | None, str, str | None]] = []
        for state in character.states:
            if not state.location:
                continue
            # 手工记录的状态可能只写了章号，没有 chapter_id：两种都要能定位到章
            chapter = by_id.get(state.chapter_id or "") or by_number.get(state.chapter_number or -1)
            day = story_time_to_day(chapter.story_time if chapter else None)
            records.append((state.chapter_number or 0, day, state.location, state.note))
        records.sort(key=lambda item: (item[0], item[1] or 0))
        for index in range(1, len(records)):
            prev, current = records[index - 1], records[index]
            if not prev[2] or not current[2] or prev[2] == current[2]:
                continue
            if prev[1] is None or current[1] is None or current[1] != prev[1]:
                continue
            issues.append(
                _issue(
                    "LOCATION_JUMP",
                    "warning",
                    f"{character.name}在同一天（{format_chapter_ref(current[0])}）出现在「{current[2]}」，"
                    f"而 {format_chapter_ref(prev[0])} 还在「{prev[2]}」",
                    subject=character.name,
                    evidence=[
                        {
                            "source_chapter": format_chapter_ref(prev[0]),
                            "quote": prev[2],
                            "detail": prev[3] or "上一次位置记录",
                        },
                        {
                            "source_chapter": format_chapter_ref(current[0]),
                            "quote": current[2],
                            "detail": current[3] or "本次位置记录",
                        },
                    ],
                    suggestion="同一天跨地需要交代移动方式与耗时；否则把两处的时间或地点改到合理",
                )
            )
    return issues


def check_commitment_breaches(session: Session, novel: Novel) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for item in commitment_service.evaluate(session, novel):
        if item["status"] != "OVERDUE":
            continue
        issues.append(
            _issue(
                "COMMITMENT_OVERDUE",
                "warning",
                f"承诺未兑现：{item['what'][:60]}（期限 {item['deadline_text'] or '未写明'}，"
                f"到期 {item['due_story_time'] or '未知'}，第{item['breach_chapter']}章已越过）",
                subject=item["who"] or "承诺",
                evidence=item["evidence"],
                suggestion="在后续章节兑现它，或在承诺账本里标记为「已放弃」并说明原因",
            )
        )
    return issues


def check_shadow_characters(session: Session, novel: Novel) -> list[dict[str, Any]]:
    """人物档案与叙事脱节：登记了却从未出场、或出场后被长期遗忘。"""
    issues: list[dict[str, Any]] = []
    chapters = _chapters(session, novel.id)
    if not chapters:
        return issues
    frontier = max(chapter.chapter_number for chapter in chapters)
    for character in session.scalars(select(Character).where(Character.novel_id == novel.id)):
        last = character.last_appearance or character.first_appearance
        if last is None:
            issues.append(
                _issue(
                    "CHARACTER_UNUSED",
                    "warning",
                    f"{character.name}已建档但没有任何出场记录",
                    subject=character.name,
                    evidence=[{"source_chapter": "—", "quote": None, "detail": "人物档案缺少首次出场"}],
                    suggestion="补上首次出场章号，或删除多余的人物档案",
                )
            )
            continue
        gap = frontier - last
        if gap >= 15:
            issues.append(
                _issue(
                    "CHARACTER_DORMANT",
                    "warning",
                    f"{character.name}自{format_chapter_ref(last)}起已 {gap} 章未出场",
                    subject=character.name,
                    evidence=[
                        {
                            "source_chapter": format_chapter_ref(last),
                            "quote": character.current_location,
                            "detail": f"最后出场：第{last}章，当前状态 {character.current_status}",
                        }
                    ],
                    suggestion="给这条线一次回应，或把人物标记为已退场",
                )
            )
    return issues


# --------------------------------------------------------------------------- 汇总
def check_invariants(session: Session, novel: Novel) -> dict[str, Any]:
    chapters = _chapters(session, novel.id)
    facts = _canon_facts(session, novel.id)
    issues: list[dict[str, Any]] = []
    issues.extend(check_time_monotonic(chapters))
    issues.extend(check_realm_regression(facts))
    issues.extend(check_exclusive_possession(facts))
    issues.extend(check_knowledge_shrink(facts))
    issues.extend(check_location_continuity(session, novel))
    issues.extend(check_commitment_breaches(session, novel))
    issues.extend(check_shadow_characters(session, novel))

    errors = [issue for issue in issues if issue["level"] == "error"]
    warnings = [issue for issue in issues if issue["level"] != "error"]
    codes = Counter(issue["code"] for issue in issues)
    return {
        "novel_id": novel.id,
        "errors": errors,
        "warnings": warnings,
        "codes": dict(codes.most_common()),
        "checked": {
            "chapters": len(chapters),
            "facts": len(facts),
            "characters": len(
                list(session.scalars(select(Character).where(Character.novel_id == novel.id)))
            ),
        },
    }


def run_invariants(session: Session, novel: Novel, *, persist: bool = True) -> dict[str, Any]:
    report = check_invariants(session, novel)
    report["failed_codes"] = sorted(report["codes"])
    if persist:
        record = InvariantReport(
            novel_id=novel.id,
            errors=len(report["errors"]),
            warnings=len(report["warnings"]),
            codes=report["codes"],
            issues=report["errors"] + report["warnings"],
        )
        session.add(record)
        session.flush()
        report["report_id"] = record.id
        report["created_at"] = record.created_at.isoformat()
    return report


def latest_report(session: Session, novel_id: str) -> InvariantReport | None:
    return session.scalar(
        select(InvariantReport)
        .where(InvariantReport.novel_id == novel_id)
        .order_by(InvariantReport.created_at.desc())
    )


def level_of(value: str | None) -> int | None:
    """给外部（例如规划时校验修为目标）用的等级换算。"""
    return realm_level(value) or cn_number_to_int(value or "")
