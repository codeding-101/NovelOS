"""V0.7 读者数据回环：导入平台后台的章节数据，验证我们的判定对不对。

这一层的意义：文本层阈值原本全是经验值。作者说「读者是最好的测试人员」，
那就把读者表现收回来 —— 判定偏弱的章是否真的掉读者、我们漏了哪一章。
"""

from __future__ import annotations

from app.config import settings
from app.services import publish_service, reader_service


def test_parse_rows_accepts_tab_comma_and_spaces():
    text = (
        "第1章\t1200\t23.5%\t18%\t12.5\t4\n"
        "2, 980, 0.198\n"
        "3   1500   31.2%\n"
        "章号\t阅读人数\t完读率\n"
        "这一行没有章号\n"
    )
    rows = reader_service.parse_reader_rows(text)
    assert [row["chapter_number"] for row in rows] == [1, 2, 3]
    assert rows[0]["reads"] == 1200
    assert rows[0]["completion_rate"] == 0.235, "百分号要归一化"
    assert rows[1]["completion_rate"] == 0.198
    assert rows[2]["reads"] == 1500 and rows[2]["completion_rate"] == 0.312
    assert rows[0]["raw"]["completion_rate"] == "23.5%", "原始行要留下，便于核对抄错列"


def test_import_overwrites_same_chapter(session, novel, chapters):
    first = reader_service.import_rows(session, novel, "第1章\t100\t20%\n第2章\t90\t18%")
    session.commit()
    assert first["imported"] == 2

    again = reader_service.import_rows(session, novel, "第1章\t500\t35%")
    session.commit()
    assert again["imported"] == 1

    rows = reader_service.import_rows(session, novel, "")["message"]
    assert "没解析出任何一行" in rows

    stored = reader_service.reader_analysis(session, novel)["chapters"]
    chapter_one = next(item for item in stored if item["chapter_number"] == 1)
    assert chapter_one["reads"] == 500 and chapter_one["completion_rate"] == 0.35, "同章号覆盖"


def test_analysis_flags_missed_and_false_alarms(session, novel, chapters):
    """造一组数据：一章我们报警但读者没跑，一章我们没报警但读者掉了。"""
    lines = []
    for chapter in chapters.values():
        number = chapter.chapter_number
        if number == 2:  # 我们把第 2 章判为弱（章末钩子偏弱），但读者没跑
            lines.append(f"{number}\t1000\t40%")
        elif number == 5:  # 我们没报警，但完读率明显低于全书平均
            lines.append(f"{number}\t800\t5%")
        else:
            lines.append(f"{number}\t1000\t30%")
    reader_service.import_rows(session, novel, "\n".join(lines))
    session.commit()

    report = reader_service.reader_analysis(session, novel)
    assert report["coverage"]["with_data"] == len(chapters)
    assert report["book_completion_rate"] > 0

    missed_numbers = {item["chapter_number"] for item in report["missed"]}
    assert 5 in missed_numbers, "完读率明显偏低且我没报警 → 应当进漏报"
    assert 2 in {item["chapter_number"] for item in report["false_alarms"]}, "报警了但读者没跑 → 误报"
    assert report["suggestions"], "要给可执行的校准建议"

    drops = report["drop_chapters"]
    assert drops, "阅读人数从 1000 掉到 800 应当被记下来"
    assert drops[0]["chapter_number"] == 5


def test_analysis_without_data_says_so(session, novel):
    report = reader_service.reader_analysis(session, novel)
    assert report["coverage"]["with_data"] == 0
    assert "经验值" in report["verdict"]
    assert report["suggestions"]


def test_reader_metrics_api(client, novel, chapters):
    response = client.post(
        f"/api/novels/{novel.id}/reader-metrics",
        json={"text": "第1章\t1000\t30%\n第2章\t950\t28%"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 2

    analysis = client.get(f"/api/novels/{novel.id}/reader-metrics").json()
    assert analysis["coverage"] == {"with_data": 2, "total": len(chapters)}
    assert analysis["book_completion_rate"] > 0

    raw = client.get(f"/api/novels/{novel.id}/reader-metrics/raw").json()
    assert raw[0]["raw"]["line"].startswith("第1章")

    bad = client.post(f"/api/novels/{novel.id}/reader-metrics", json={"text": "没有章号的行"})
    assert bad.status_code == 400

    cleared = client.delete(f"/api/novels/{novel.id}/reader-metrics?chapter_number=1")
    assert cleared.json()["deleted"] == 1
    assert client.get(f"/api/novels/{novel.id}/reader-metrics").json()["coverage"]["with_data"] == 1


def test_novel_publish_settings_are_editable_and_used(client, novel, chapters):
    """每章字数与日更目标按这本书的设置来，作者不用等别人改代码。"""
    patched = client.patch(
        f"/api/novels/{novel.id}",
        json={"chapter_words_min": 2500, "chapter_words_max": 3500, "daily_words_target": 6000},
    ).json()
    assert patched["chapter_words_min"] == 2500 and patched["daily_words_target"] == 6000

    chapter = chapters[5]
    report = client.post(f"/api/chapters/{chapter.id}/publish-check").json()
    assert report["words_per_chapter"] == [2500, 3500], "发布检查按本书设置的字数区间"

    draft = client.post(
        f"/api/novels/{novel.id}/publish-check", json={"text": "第1章 试\n\n" + "他走了很久。" * 300}
    ).json()
    assert draft["words_per_chapter"] == [2500, 3500]


def test_extra_risk_words_file_is_optional(tmp_path, monkeypatch):
    """作者可以自己加风险词，不用改代码。"""
    assert publish_service.load_extra_risk_words() == {}

    extra_file = settings.data_dir / "risk_words.json"
    existed = extra_file.exists()
    original = extra_file.read_text(encoding="utf-8") if existed else None
    try:
        extra_file.write_text('{"站外引流": ["加微", ["私信领取", "别引导站外"]]}', encoding="utf-8")
        table = publish_service.load_extra_risk_words()
        assert "站外引流" in table
        words = {word for word, _advice in table["站外引流"]}
        assert {"加微", "私信领取"} <= words

        hits = publish_service.find_risk_words("想看的加微，或者私信领取后续。")
        assert any(hit["word"] == "加微" for hit in hits)

        extra_file.write_text("{ 这不是 json", encoding="utf-8")
        assert publish_service.load_extra_risk_words() == {}, "坏文件不能把检查搞崩"
    finally:
        if existed and original is not None:
            extra_file.write_text(original, encoding="utf-8")
        else:
            extra_file.unlink(missing_ok=True)
