"""与 Obsidian 笔记库（vault）的对接：素材进得来，设定库出得去。

两个方向，各自的难点不一样：

- **vault → 碎片**：作者脑子里的东西大多先落在笔记里。导入要幂等 —— 同一篇笔记反复导入
  不能变成一堆重复碎片，所以按来源路径认，内容变了才更新。frontmatter 可以指定类型、
  意图、标签、相关人物、目标章号，不写就按文件夹与标签推断。
- **设定库 → vault**：把人物、事件、伏笔、Canon、世界观、大纲导出成带双链的笔记，
  作者能在 Obsidian 里用自己的方式浏览。这里的风险是**覆盖掉作者在库里的批注**，
  所以导出时记住写入内容的哈希：文件被改过就不动它，另存一份 .conflict.md 让人自己看。

vault 路径默认从 Obsidian 自己的配置里探测，不用手填。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CanonFact, Chapter, Character, Event, Foreshadowing, Fragment, Novel, WorldRule
from app.services import fragment_service

#: 集成配置放在数据目录，跟 risk_words.json 一个路子：作者可读可改
INTEGRATION_FILE = "integrations.json"
#: 默认收件箱与导出目录（相对 vault 根）
DEFAULT_INBOX = "素材"
DEFAULT_EXPORT_DIR = "NovelOS"
#: frontmatter 里用来标记「这属于 NovelOS」的键
MARKER_KEY = "novelos"


@dataclass
class NoteDoc:
    """一篇笔记：路径、frontmatter、正文。"""

    path: Path
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        value = str(self.meta.get("title") or "").strip()
        if value:
            return value
        heading = next(
            (line.lstrip("#").strip() for line in self.body.splitlines() if line.startswith("#")),
            "",
        )
        return heading or self.path.stem


# --------------------------------------------------------------------------- 配置
def integrations_path() -> Path:
    return settings.data_dir / INTEGRATION_FILE


def load_integrations() -> dict[str, Any]:
    path = integrations_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_integrations(payload: dict[str, Any]) -> dict[str, Any]:
    current = load_integrations()
    current.update(payload)
    path = integrations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def obsidian_config_path() -> Path:
    return Path.home() / "AppData" / "Roaming" / "obsidian" / "obsidian.json"


def detect_vaults() -> list[dict[str, Any]]:
    """从 Obsidian 自己的配置里读出已知的库，省得让作者手抄路径。"""
    path = obsidian_config_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    vaults = (raw.get("vaults") or {}) if isinstance(raw, dict) else {}
    found: list[dict[str, Any]] = []
    for vault_id, info in vaults.items():
        vault_path = Path(str((info or {}).get("path") or ""))
        if not str(vault_path):
            continue
        found.append(
            {
                "id": vault_id,
                "path": str(vault_path),
                "exists": vault_path.exists(),
                "open": bool((info or {}).get("open")),
            }
        )
    found.sort(key=lambda item: (not item["open"], item["path"]))
    return found


def resolve_vault(explicit: str | None = None) -> Path | None:
    """确定用哪个库：显式给的 > 环境变量 > 配置里存的 > Obsidian 当前打开的那个。"""
    candidates = [explicit, settings.obsidian_vault, load_integrations().get("vault")]
    for item in candidates:
        if item and Path(str(item)).exists():
            return Path(str(item))
    detected = [item for item in detect_vaults() if item["exists"]]
    return Path(detected[0]["path"]) if detected else None


def vault_status(vault_path: str | None = None, inbox: str | None = None) -> dict[str, Any]:
    """看一眼库现在什么样：能不能写、收件箱里有多少笔记、之前导出过什么。"""
    config = load_integrations()
    vault = resolve_vault(vault_path)
    inbox_name = inbox or config.get("inbox") or DEFAULT_INBOX
    export_name = config.get("export_dir") or DEFAULT_EXPORT_DIR
    if vault is None:
        return {
            "connected": False,
            "vault": "",
            "detected": detect_vaults(),
            "message": "没找到 Obsidian 库：装好 Obsidian 并打开过一次，或手动指定路径",
        }
    notes = sorted(vault.rglob("*.md")) if vault.exists() else []
    inbox_path = vault / inbox_name
    return {
        "connected": True,
        "vault": str(vault),
        "inbox": inbox_name,
        "inbox_exists": inbox_path.exists(),
        "inbox_notes": len(list(inbox_path.rglob("*.md"))) if inbox_path.exists() else 0,
        "export_dir": export_name,
        "export_exists": (vault / export_name).exists(),
        "total_notes": len(notes),
        "exported": len(load_integrations().get("exports") or {}),
        "detected": detect_vaults(),
        "message": f"已连接 {vault}",
    }


# --------------------------------------------------------------------------- vault → 碎片
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
_TAG_RE = re.compile(r"(?<![\w#])#([\w\u4e00-\u9fa5/-]+)")


def parse_note(path: Path) -> NoteDoc | None:
    """读一篇笔记，解析 YAML frontmatter（只认简单键值，不依赖 yaml 库）与行内标签。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta: dict[str, Any] = {}
    body = text
    match = _FRONTMATTER_RE.match(text)
    if match:
        for line in match.group(1).splitlines():
            if ":" not in line or line.strip().startswith("#"):
                continue
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip().strip('"').strip("'")
        body = text[match.end() :]
    tags = [tag for tag in _TAG_RE.findall(body)]
    for key in ("tags", "tag"):
        value = meta.get(key)
        if not value:
            continue
        raw = str(value).strip().strip("[]")
        tags.extend(part.strip().strip('"').strip("'") for part in re.split(r"[,\s]+", raw) if part.strip())
    return NoteDoc(path=path, meta=meta, body=body, tags=sorted(set(tags)))


def _split_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    text = str(value).strip().strip("[]")
    return [part.strip().strip('"').strip("'") for part in re.split(r"[,\s、]+", text) if part.strip()]


def _note_kind(note: NoteDoc) -> str:
    for key in ("novelos_kind", "kind", "类型"):
        value = str(note.meta.get(key) or "").strip().upper()
        if value in fragment_service.KINDS:
            return value
    for tag in note.tags:
        if tag.upper() in fragment_service.KINDS:
            return tag.upper()
    return "WHIM"


def _is_material(note: NoteDoc, tag_filter: list[str]) -> bool:
    """收件箱里的东西默认都收；在别处则要求有标记（frontmatter 的 novelos 键或指定标签）。"""
    if tag_filter and any(tag in tag_filter for tag in note.tags):
        return True
    return MARKER_KEY in note.meta or "novelos" in note.tags


def import_notes(
    session: Session,
    novel: Novel,
    *,
    vault_path: str | None = None,
    inbox: str | None = None,
    tags: list[str] | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """把库里收件箱的笔记导入成碎片。幂等：同一路径第二次导入只更新正文，不新建。"""
    config = load_integrations()
    vault = resolve_vault(vault_path)
    if vault is None:
        return {"imported": 0, "updated": 0, "skipped": 0, "notes": [], "message": "没有可用的 Obsidian 库"}

    inbox_name = inbox or config.get("inbox") or DEFAULT_INBOX
    inbox_path = vault / inbox_name
    if not inbox_path.exists():
        return {
            "imported": 0,
            "updated": 0,
            "skipped": 0,
            "notes": [],
            "message": f"收件箱不存在：{inbox_path}（在库里建一个「{inbox_name}」文件夹，笔记放进去即可）",
        }

    tag_filter = tags or [str(item) for item in (config.get("tags") or [])]

    existing = {
        item.source_path: item
        for item in session.scalars(
            select(Fragment).where(Fragment.novel_id == novel.id, Fragment.source_path != "")
        )
    }
    imported: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    for path in sorted(inbox_path.rglob("*.md"))[:limit]:
        note = parse_note(path)
        if note is None:
            continue
        body = note.body.strip()
        if not body and not note.title:
            skipped.append(str(path))
            continue
        current = existing.get(str(path))
        if current is not None:
            if current.text.strip() != body:
                current.text = body
                current.title = current.title or note.title
                updated.append(str(path))
            else:
                skipped.append(str(path))
            continue
        created = fragment_service.create_fragment(
            session,
            novel,
            fragment_service.FragmentCreate(
                text=body or note.title,
                title=note.title,
                kind=_note_kind(note),
                intent=str(note.meta.get("intent") or note.meta.get("意图") or ""),
                tags=note.tags,
                related_characters=_split_list(
                    note.meta.get("characters") or note.meta.get("人物")
                ),
                target_chapter=int(note.meta["chapter"])
                if str(note.meta.get("chapter") or "").isdigit()
                else None,
                origin="OBSIDIAN",
                notes=f"来自 {path.name}",
            ),
        )
        created.source_path = str(path)
        imported.append(str(path))

    session.flush()
    save_integrations({"vault": str(vault), "inbox": inbox_name, "tags": tag_filter})
    return {
        "imported": len(imported),
        "updated": len(updated),
        "skipped": len(skipped),
        "notes": imported,
        "message": (
            f"从「{inbox_name}」导入：新增 {len(imported)} 条、更新 {len(updated)} 条、"
            f"未变 {len(skipped)} 条"
        ),
    }


# --------------------------------------------------------------------------- 设定库 → vault
def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _text(value: Any) -> str:
    """把字段渲染成正文里的文字：列表用「；」连起来，别把 Python 的方括号与引号带进笔记。"""
    if value in (None, ""):
        return ""
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return "；".join(items)
    if isinstance(value, dict):
        return "；".join(f"{key}：{_text(item)}" for key, item in value.items())
    return str(value).strip()


def _note_text(title: str, front: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for key, value in front.items():
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(str(item) for item in value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(body.strip())
    lines.append("")
    return "\n".join(lines)


def _write_note(path: Path, text: str, exports: dict[str, Any]) -> str:
    """写一篇笔记：文件被人工改过就不覆盖，另存 .conflict.md。返回状态。"""
    record = exports.get(str(path)) or {}
    if path.exists():
        current = path.read_text(encoding="utf-8")
        expected = record.get("hash")
        if expected and _hash(current) != expected:
            conflict = path.with_suffix(".conflict.md")
            conflict.write_text(text, encoding="utf-8")
            return "conflict"
        if not expected and current.strip() != text.strip():
            conflict = path.with_suffix(".conflict.md")
            conflict.write_text(text, encoding="utf-8")
            return "conflict"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    exports[str(path)] = {"hash": _hash(text), "at": datetime.now(timezone.utc).isoformat()}
    return "written"


def export_bible(
    session: Session,
    novel: Novel,
    *,
    vault_path: str | None = None,
    subdir: str | None = None,
) -> dict[str, Any]:
    """把设定库导出成带双链的笔记，供在 Obsidian 里浏览。不覆盖被改过的文件。"""
    config = load_integrations()
    vault = resolve_vault(vault_path)
    if vault is None:
        return {"written": 0, "conflicts": 0, "files": [], "message": "没有可用的 Obsidian 库"}

    export_dir = vault / (subdir or config.get("export_dir") or DEFAULT_EXPORT_DIR) / (novel.title or novel.id)
    exports: dict[str, Any] = dict(config.get("exports") or {})
    written: list[str] = []
    conflicts: list[str] = []

    def emit(relative: str, title: str, front: dict[str, Any], body: str) -> None:
        path = export_dir / relative
        status = _write_note(path, _note_text(title, front, body), exports)
        if status == "conflict":
            conflicts.append(str(path.with_suffix(".conflict.md")))
        else:
            written.append(str(path))

    # 索引篇：全书概览 + 指向各设定页
    characters = list(session.scalars(select(Character).where(Character.novel_id == novel.id)))
    events = list(
        session.scalars(select(Event).where(Event.novel_id == novel.id).order_by(Event.chapter_number))
    )
    facts = list(session.scalars(select(CanonFact).where(CanonFact.novel_id == novel.id)))
    foreshadowing = list(
        session.scalars(select(Foreshadowing).where(Foreshadowing.novel_id == novel.id))
    )
    rules = list(session.scalars(select(WorldRule).where(WorldRule.novel_id == novel.id)))
    chapters = list(
        session.scalars(
            select(Chapter).where(Chapter.novel_id == novel.id).order_by(Chapter.chapter_number)
        )
    )

    emit(
        "索引.md",
        f"{novel.title} · 设定索引",
        {"novelos": "index", "novel": novel.title, "字数": novel.word_count, "章数": novel.chapter_count},
        "\n".join(
            [
                f"- 人物：{len(characters)} 位 → [[人物索引]]",
                f"- 事件：{len(events)} 条 → [[事件索引]]",
                f"- 伏笔：{len(foreshadowing)} 条 → [[伏笔]]",
                f"- Canon 事实：{len(facts)} 条 → [[Canon]]",
                f"- 世界观规则：{len(rules)} 条 → [[世界观]]",
                "- 大纲：见 [[全书大纲]]",
                "",
                "> 这些笔记由 NovelOS 导出。在库里改动过的那一篇不会被自动覆盖，",
                "> 重导出时会把新内容另存为 `.conflict.md` 让你自己合并。",
            ]
        ),
    )

    if (novel.outline or "").strip():
        emit(
            "全书大纲.md",
            "全书大纲",
            {"novelos": "outline", "novel": novel.title},
            novel.outline,
        )
    if (novel.worldview or "").strip():
        rule_lines = [
            f"- **{rule.subject or rule.name}**（{rule.rule_type}）：{rule.description}"
            for rule in rules
        ]
        body = novel.worldview
        if rule_lines:
            body = body + "\n\n## 规则\n\n" + "\n".join(rule_lines)
        emit("世界观.md", "世界观", {"novelos": "worldview", "novel": novel.title}, body)

    emit(
        "人物索引.md",
        "人物索引",
        {"novelos": "characters", "novel": novel.title},
        "\n".join(
            f"- [[{item.name}]]：{item.current_status or '状态未知'}｜{item.current_location or '位置未知'}"
            f"｜{_text(item.goals) or '目标未写'}"
            for item in characters
        )
        or "（还没有建档的人物）",
    )
    for character in characters:
        facts_of = [fact for fact in facts if fact.subject == character.name]
        emit(
            f"人物/{character.name}.md",
            character.name,
            {
                "novelos": "character",
                "人物": character.name,
                "状态": character.current_status,
                "位置": character.current_location,
                "首次出场": f"第{character.first_appearance or '?'}章",
            },
            "\n".join(
                [
                    character.description or "",
                    "",
                    f"**性格**：{_text(character.personality) or '—'}",
                    f"**背景**：{_text(character.background) or '—'}",
                    f"**目标**：{_text(character.goals) or '—'}",
                    f"**顾虑**：{_text(character.fears) or '—'}",
                    f"**当前位置**：{_text(character.current_location) or '—'}",
                    "",
                    "## 已确认的设定",
                    "\n".join(
                        f"- {fact.predicate}：{fact.object}（第{fact.source_chapter or '?'}章）"
                        for fact in facts_of
                    )
                    or "（暂无）",
                ]
            ),
        )

    emit(
        "事件索引.md",
        "事件索引",
        {"novelos": "events", "novel": novel.title},
        "\n".join(
            f"- 第{item.chapter_number}章｜{item.time or '时间未记'}｜{item.location or '地点未记'}："
            f"{(item.description or '')[:80]}"
            for item in events
        )
        or "（还没有事件）",
    )
    emit(
        "伏笔.md",
        "伏笔",
        {"novelos": "foreshadowing", "novel": novel.title},
        "\n".join(
            f"- **{item.name}**｜{item.status}"
            f"｜首现第{item.first_chapter}章｜最近强化第{item.last_reinforced_chapter or '—'}章"
            f"｜{item.description or ''}"
            for item in foreshadowing
        )
        or "（还没有伏笔）",
    )
    emit(
        "Canon.md",
        "Canon 事实",
        {"novelos": "canon", "novel": novel.title},
        "\n".join(
            f"- `{fact.subject}` **{fact.predicate}** = {fact.object}"
            f"｜{fact.status}｜第{fact.source_chapter or '?'}章"
            for fact in facts
        )
        or "（还没有 Canon 事实）",
    )
    if chapters:
        emit(
            "章节清单.md",
            "章节清单",
            {"novelos": "chapters", "novel": novel.title},
            "\n".join(
                f"- 第{chapter.chapter_number}章 {chapter.title or ''}"
                f"｜{chapter.word_count} 字｜{chapter.story_time or ''}｜{chapter.location or ''}"
                for chapter in chapters
            ),
        )

    save_integrations({"vault": str(vault), "exports": exports})
    return {
        "written": len(written),
        "conflicts": len(conflicts),
        "files": written,
        "conflict_files": conflicts,
        "directory": str(export_dir),
        "message": (
            f"导出 {len(written)} 篇到 {export_dir.name}"
            + (f"，另有 {len(conflicts)} 篇你在库里改过，已另存为 .conflict.md" if conflicts else "")
        ),
    }
