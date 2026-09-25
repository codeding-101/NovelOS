"""V0.9 Obsidian 对接：素材进得来、设定出得去，两边都不许出错。

- 导入：frontmatter/标签/文件夹三种识别方式、幂等（重复导入不新建）、内容变了才更新；
- 导出：设定库变成带双链的笔记；**作者在库里改过的那篇不许被覆盖**，另存 .conflict.md；
- 配置：库路径能从 Obsidian 自己的配置探测，也能手填；收件箱不存在时给可执行的提示。
"""

from __future__ import annotations

import json

from app.services import obsidian_service


def _make_vault(tmp_path, name: str = "vault"):
    vault = tmp_path / name
    (vault / "素材").mkdir(parents=True)
    return vault


def test_parse_note_reads_frontmatter_tags_and_heading(tmp_path):
    vault = _make_vault(tmp_path)
    note = vault / "素材" / "断崖下的东西.md"
    note.write_text(
        "---\n"
        "kind: SCENE\n"
        "intent: 想让读者怀疑赵铁山没死\n"
        "characters: 林默, 苏月宁\n"
        "chapter: 21\n"
        "tags: [悬疑, 崖]\n"
        "---\n"
        "# 断崖下的东西\n\n"
        "雾散之前，先看到的是那只手。 #关键场景\n",
        encoding="utf-8",
    )
    doc = obsidian_service.parse_note(note)
    assert doc is not None
    assert doc.title == "断崖下的东西", "标题取 frontmatter 或首个标题行"
    assert doc.meta["kind"] == "SCENE"
    assert {"悬疑", "崖", "关键场景"} <= set(doc.tags), "frontmatter 与本体的标签都要收"
    assert "先看到的是那只手" in doc.body


def test_import_is_idempotent_and_updates_on_change(session, novel, tmp_path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(
        obsidian_service, "resolve_vault", lambda explicit=None: vault
    )
    note = vault / "素材" / "灵感一.md"
    note.write_text("他说要把刀留下。\n", encoding="utf-8")

    first = obsidian_service.import_notes(session, novel, vault_path=str(vault))
    session.commit()
    assert first["imported"] == 1 and first["updated"] == 0

    second = obsidian_service.import_notes(session, novel, vault_path=str(vault))
    session.commit()
    assert second["imported"] == 0 and second["skipped"] == 1, "没变就不动它"

    note.write_text("他说要把刀留下，然后把刀鞘也放下了。\n", encoding="utf-8")
    third = obsidian_service.import_notes(session, novel, vault_path=str(vault))
    session.commit()
    assert third["updated"] == 1, "内容变了才更新"

    from sqlalchemy import select

    from app.models import Fragment

    fragments = list(session.scalars(select(Fragment).where(Fragment.novel_id == novel.id)))
    assert len(fragments) == 1, "同一篇笔记只能有一条碎片"
    assert fragments[0].origin == "OBSIDIAN"
    assert fragments[0].source_path.endswith("灵感一.md")
    assert "刀鞘也放下了" in fragments[0].text


def test_import_outside_inbox_needs_a_marker(session, novel, tmp_path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(obsidian_service, "resolve_vault", lambda explicit=None: vault)
    # 收件箱外的笔记要被忽略（除非带 novelos 标记）——不能把整个库都当素材吸进来
    (vault / "日记.md").write_text("今天写了三千字。\n", encoding="utf-8")
    result = obsidian_service.import_notes(session, novel, vault_path=str(vault))
    assert result["imported"] == 0, "收件箱以外的笔记不该被自动导入"


def test_missing_inbox_gives_actionable_message(session, novel, tmp_path, monkeypatch):
    vault = tmp_path / "empty_vault"
    vault.mkdir()
    monkeypatch.setattr(obsidian_service, "resolve_vault", lambda explicit=None: vault)
    result = obsidian_service.import_notes(session, novel, vault_path=str(vault))
    assert result["imported"] == 0
    assert "素材" in result["message"] and "建一个" in result["message"]


def test_export_writes_linked_notes_and_never_clobbers_edits(session, novel, tmp_path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(obsidian_service, "resolve_vault", lambda explicit=None: vault)

    first = obsidian_service.export_bible(session, novel, vault_path=str(vault))
    session.commit()
    assert first["written"] > 0
    directory = vault / "NovelOS" / (novel.title or novel.id)
    assert directory.exists()

    index = (directory / "索引.md").read_text(encoding="utf-8")
    assert "[[人物索引]]" in index and "[[Canon]]" in index, "索引要能双链跳转"
    assert "novelos: index" in index, "frontmatter 里要有标记，方便以后识别"
    characters = list((directory / "人物").glob("*.md"))
    assert characters, "人物应当各成一篇"
    text = characters[0].read_text(encoding="utf-8")
    assert "---" in text and "## 已确认的设定" in text

    # 作者在 Obsidian 里改了其中一篇 —— 重导出必须保住他的改动
    edited = characters[0]
    author_note = "我自己在这里写了批注。\n"
    edited.write_text(author_note, encoding="utf-8")
    second = obsidian_service.export_bible(session, novel, vault_path=str(vault))
    session.commit()
    assert second["conflicts"] >= 1
    assert edited.read_text(encoding="utf-8") == author_note, "改过的文件不许被覆盖"
    assert edited.with_suffix(".conflict.md").exists(), "新内容另存为 .conflict.md"
    assert edited.stem in second["conflict_files"][0], "冲突清单要指出是哪一篇"

    # 没改过的那些照常更新
    third = obsidian_service.export_bible(session, novel, vault_path=str(vault))
    session.commit()
    assert third["written"] > 0


def test_vault_status_reports_inbox_and_config(session, novel, tmp_path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(obsidian_service, "resolve_vault", lambda explicit=None: vault)
    (vault / "素材" / "a.md").write_text("一\n", encoding="utf-8")
    (vault / "素材" / "b.md").write_text("二\n", encoding="utf-8")

    status = obsidian_service.vault_status(str(vault))
    assert status["connected"] is True
    assert status["inbox"] == "素材" and status["inbox_exists"] is True
    assert status["inbox_notes"] == 2


def test_sync_outline_reads_the_note_in_a_book_named_folder(session, novel, tmp_path, monkeypatch):
    """作者在 Obsidian 里改大纲 → 同步进系统。书名号文件夹与裸书名都要认。"""
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(obsidian_service, "resolve_vault", lambda explicit=None: vault)
    folder = vault / f"《{novel.title}》"
    folder.mkdir()
    (folder / "大纲.md").write_text("# 新大纲\n\n第一卷：起。\n", encoding="utf-8")

    novel.outline = "旧大纲"
    session.flush()
    result = obsidian_service.sync_outline(session, novel, vault_path=str(vault))
    session.commit()
    assert result["synced"] is True and result["changed"] is True
    assert novel.outline.startswith("# 新大纲")

    again = obsidian_service.sync_outline(session, novel, vault_path=str(vault))
    assert again["synced"] is True and again["changed"] is False, "一致时不重复写"

    bare = vault / novel.title
    bare.mkdir()
    (bare / "大纲.md").write_text("裸书名文件夹里的版本", encoding="utf-8")
    (folder / "大纲.md").unlink()
    third = obsidian_service.sync_outline(session, novel, vault_path=str(vault))
    assert third["changed"] is True and novel.outline == "裸书名文件夹里的版本"


def test_sync_outline_reports_missing_note(session, novel, tmp_path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(obsidian_service, "resolve_vault", lambda explicit=None: vault)
    result = obsidian_service.sync_outline(session, novel, vault_path=str(vault))
    assert result["synced"] is False
    assert "没找到" in result["message"]


def test_sync_outline_ignores_empty_note(session, novel, tmp_path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(obsidian_service, "resolve_vault", lambda explicit=None: vault)
    folder = vault / f"《{novel.title}》"
    folder.mkdir()
    (folder / "大纲.md").write_text("   \n", encoding="utf-8")
    novel.outline = "别被清空"
    session.flush()
    result = obsidian_service.sync_outline(session, novel, vault_path=str(vault))
    assert result["synced"] is False and novel.outline == "别被清空", "空笔记不能把大纲清掉"


def test_sync_outline_api(client, novel, tmp_path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(obsidian_service, "resolve_vault", lambda explicit=None: vault)
    folder = vault / f"《{novel.title}》"
    folder.mkdir()
    (folder / "大纲.md").write_text("接口同步过来的大纲", encoding="utf-8")

    response = client.post(f"/api/novels/{novel.id}/obsidian/sync-outline", json={"vault": str(vault)})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["synced"] is True and payload["changed"] is True
    assert client.get(f"/api/novels/{novel.id}").json()["outline"] == "接口同步过来的大纲"


def test_api_import_export_and_config(client, novel, tmp_path, monkeypatch):
    vault = _make_vault(tmp_path)
    monkeypatch.setattr(obsidian_service, "resolve_vault", lambda explicit=None: vault)
    (vault / "素材" / "开篇的画面.md").write_text(
        "---\nkind: IMAGE\n---\n# 开篇的画面\n\n雨里的石阶，一个人蹲着擦刀。\n", encoding="utf-8"
    )

    status = client.get("/api/obsidian").json()
    assert status["connected"] is True

    configured = client.post("/api/obsidian", json={"inbox": "素材", "export_dir": "NovelOS"}).json()
    assert configured["inbox"] == "素材"

    imported = client.post(
        f"/api/novels/{novel.id}/obsidian/import", json={"vault": str(vault)}
    ).json()
    assert imported["imported"] == 1, imported

    exported = client.post(
        f"/api/novels/{novel.id}/obsidian/export", json={"vault": str(vault)}
    ).json()
    assert exported["written"] > 0
    assert (vault / "NovelOS" / novel.title / "索引.md").exists()

    missing = client.post("/api/novels/nov_missing/obsidian/import", json={"vault": str(vault)})
    assert missing.status_code == 404


def test_config_is_persisted_in_the_data_dir(session, novel, tmp_path, monkeypatch):
    # 配置写进数据目录（不能用 monkeypatch 改 settings：那是 frozen dataclass）
    target = tmp_path / "integrations.json"
    monkeypatch.setattr(obsidian_service, "integrations_path", lambda: target)
    obsidian_service.save_integrations({"vault": "D:/notes/vault", "inbox": "想法"})
    stored = json.loads(target.read_text(encoding="utf-8"))
    assert stored["vault"] == "D:/notes/vault"
    assert obsidian_service.load_integrations()["inbox"] == "想法"
