"""重录核对(结批后换稿、待复核清单、下载)自测。运行: python3 -m pytest tests/ -v"""

import csv
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app


# ---------------------------------------------------------------- 夹具与辅助

@pytest.fixture()
def client(tmp_path):
    app = create_app({"TESTING": True, "DB_PATH": str(tmp_path / "t.db"),
                      "SECRET_KEY": "test"})
    return app.test_client()


def make_items(n):
    return [{"entry_no": f"E{i:03d}", "nameplate_no": f"NP-{i:03d}"}
            for i in range(1, n + 1)]


def open_and_close(client, batch_no="B1", result="ok"):
    """开批(单检查点)并结批。返回结批后的详情 JSON。"""
    client.post("/api/batches", json={
        "batch_no": batch_no, "limit_pct": 20, "checkpoints": [10],
        "items": make_items(20), "seed": "s1"})
    detail = client.get(f"/api/batches/{batch_no}").get_json()
    assert len(detail["sample"]) == 10
    for it in detail["sample"]:
        client.post(f"/api/batches/{batch_no}/results",
                    json={"entry_no": it["entry_no"], "result": result})
    detail = client.get(f"/api/batches/{batch_no}").get_json()
    assert detail["batch"]["status"] != "open"
    return detail


def upload(client, draft_no, lines, batch_no="B1"):
    text = "新条目号,铭牌编号,原条目号\n" + "\n".join(lines)
    return client.post(f"/api/batches/{batch_no}/rekey/drafts",
                       json={"draft_no": draft_no, "csv_text": text})


def drafts_of(client, batch_no="B1"):
    return client.get(f"/api/batches/{batch_no}/rekey/drafts").get_json()["drafts"]


def find_draft(client, draft_no, batch_no="B1"):
    return next(d for d in drafts_of(client, batch_no) if d["draft_no"] == draft_no)


def reasons(client, draft_no, batch_no="B1"):
    return {p["new_entry_no"]: p["reason"]
            for p in find_draft(client, draft_no, batch_no)["pending"]}


# ---------------------------------------------------------------- 入口限制

def test_rekey_rejected_while_open(client):
    client.post("/api/batches", json={
        "batch_no": "O1", "limit_pct": 20, "checkpoints": [5],
        "items": make_items(10), "seed": "s"})
    resp = upload(client, "G1", ["R1,NP-001,E001"], batch_no="O1")
    assert resp.status_code == 409
    assert "结批" in resp.get_json()["error"]
    assert drafts_of(client, "O1") == []
    # 批次/稿不存在的 404
    assert client.post("/api/batches/NOPE/rekey/drafts",
                       json={"draft_no": "G", "csv_text": "a,b,c\n"}).status_code == 404
    assert client.get("/api/batches/B1/rekey/drafts/G/x").status_code == 404


# ---------------------------------------------------------------- 沿用/待复核原因

def test_pending_reasons_cover_each_rule(client):
    detail = open_and_close(client)
    sample = detail["sample"]
    ok = next(it for it in sample if it["result"] == "ok")
    other = sample[1]
    unsampled = sorted({f"E{i:03d}" for i in range(1, 21)}
                       - {it["entry_no"] for it in sample})[0]
    lines = [
        f"R-OK,{ok['nameplate_no']},{ok['entry_no']}",          # 可沿用
        f"R-NP,NP-CHANGED,{other['entry_no']}",                 # 铭牌不逐字相同
        f"R-BLANK,NP-X,",                                        # 原条目留空
        f"R-MISS,NP-X,NO-SUCH",                                  # 原条目不存在
        f"R-UNSAMPLED,NP-X,{unsampled}",                         # 未抽中
    ]
    assert upload(client, "G1", lines).status_code == 201
    d = find_draft(client, "G1")
    assert d["row_count"] == 5 and d["inherited_count"] == 1 and d["pending_count"] == 4
    assert d["inherited"][0]["new_entry_no"] == "R-OK"
    got = {p["new_entry_no"]: p["reason"] for p in d["pending"]}
    assert "逐字相同" in got["R-NP"]
    assert "留空" in got["R-BLANK"]
    assert "不在本批原清单" in got["R-MISS"]
    assert "未被抽中" in got["R-UNSAMPLED"]


def test_err_and_unchecked_original_cannot_inherit(client):
    # 全部录错 -> 已核但结果为错误
    open_and_close(client, result="err")
    d0 = client.get("/api/batches/B1").get_json()
    err_entry = d0["sample"][0]
    assert upload(client, "G1", [f"R1,{err_entry['nameplate_no']},{err_entry['entry_no']}"]).status_code == 201
    r = reasons(client, "G1")
    assert "错误" in r["R1"]


def test_duplicate_reference_blocks_inheritance_in_both_drafts(client):
    detail = open_and_close(client)
    ok = next(it for it in sample_oks(detail))
    other = detail["sample"][1]
    upload(client, "G1", [f"R1,{ok['nameplate_no']},{ok['entry_no']}"])
    assert find_draft(client, "G1")["inherited_count"] == 1   # 唯一引用, 沿用
    # G2 重复引用同一原条目
    upload(client, "G2", [f"R2,{ok['nameplate_no']},{ok['entry_no']}",
                          f"R3,{other['nameplate_no']},{other['entry_no']}"])
    g1 = find_draft(client, "G1")
    g2 = find_draft(client, "G2")
    assert g1["pending_count"] == 1 and g1["inherited_count"] == 0
    assert "引用 2 次" in g1["pending"][0]["reason"]
    g2_pending = {p["new_entry_no"]: p["reason"] for p in g2["pending"]}
    assert "引用 2 次" in g2_pending["R2"]
    # G2 内 R3 仍唯一、正确、逐字相同 -> 沿用
    assert {i["new_entry_no"] for i in g2["inherited"]} == {"R3"}


def sample_oks(detail):
    return [it for it in detail["sample"] if it["result"] == "ok"]


def test_removing_duplicate_restores_inheritance_everywhere(client):
    detail = open_and_close(client)
    ok = sample_oks(detail)[0]
    other = detail["sample"][1]
    upload(client, "G1", [f"R1,{ok['nameplate_no']},{ok['entry_no']}"])
    upload(client, "G2", [f"R2,{ok['nameplate_no']},{ok['entry_no']}"])
    assert find_draft(client, "G1")["pending_count"] == 1
    # 整份替换 G2: 去掉重复引用
    resp = upload(client, "G2", [f"R2,{other['nameplate_no']},{other['entry_no']}"])
    assert resp.status_code == 200 and resp.get_json()["replaced"] is True
    g2 = find_draft(client, "G2")
    assert g2["version"] == 2 and g2["row_count"] == 1
    # 两稿都重新判定: 引用恢复唯一
    assert find_draft(client, "G1")["pending_count"] == 0
    assert g2["pending_count"] == 0
    assert find_draft(client, "G1")["inherited_count"] == 1


# ---------------------------------------------------------------- 解析与校验

def test_upload_validation_errors(client):
    open_and_close(client)

    def bad(draft_no, text):
        resp = client.post("/api/batches/B1/rekey/drafts",
                           json={"draft_no": draft_no, "csv_text": text})
        assert resp.status_code == 400, text
        return resp.get_json()["error"]

    assert "稿号" in bad(" ", "a,b,c\nx,y,z")
    assert "为空" in bad("G", "新条目号,铭牌编号,原条目号")
    assert "3 列" in bad("G", "R1,NP-X,E1,EXTRA")
    assert "新条目号为空" in bad("G", ",NP-X,E1")
    assert "铭牌编号为空" in bad("G", "R1,,E1")
    assert "重复" in bad("G", "R1,NP-X,E1\nR1,NP-Y,E2")
    # 解析失败不产生任何稿
    assert drafts_of(client) == []


def test_csv_variants_header_bom_chinese_comma_blank_original(client):
    open_and_close(client)
    detail = client.get("/api/batches/B1").get_json()
    ok = detail["sample"][0]
    text = (
        "﻿新条目号，铭牌编号，原条目号\n"
        f"R1，{ok['nameplate_no']}，{ok['entry_no']}\n"
        "R2,NP-BLANK,\n"
        "\n"
    )
    resp = client.post("/api/batches/B1/rekey/drafts",
                       json={"draft_no": "G9", "csv_text": text})
    assert resp.status_code == 201
    d = find_draft(client, "G9")
    assert d["row_count"] == 2
    assert d["inherited_count"] == 1
    assert d["pending"][0]["new_entry_no"] == "R2"


def test_replacement_failure_keeps_everything_intact(client):
    detail = open_and_close(client)
    ok = sample_oks(detail)[0]
    upload(client, "G1", [f"R1,{ok['nameplate_no']},{ok['entry_no']}"])
    before = find_draft(client, "G1")
    # 替换稿内容非法 -> 400, 原 G1 原样保留(版本、行数、清单都不变)
    bad_text = "新条目号,铭牌编号,原条目号\nR1,NP-X,E1\nR1,NP-Y,E2"  # 新条目号重复
    resp = client.post("/api/batches/B1/rekey/drafts",
                       json={"draft_no": "G1", "csv_text": bad_text})
    assert resp.status_code == 400 and "重复" in resp.get_json()["error"]
    after = find_draft(client, "G1")
    assert after["version"] == before["version"]
    assert after["row_count"] == before["row_count"]
    assert after["token"] == before["token"]


# ---------------------------------------------------------------- 下载与版本核对

def test_download_matches_page_and_stale_token_rejected(client):
    open_and_close(client)
    detail = client.get("/api/batches/B1").get_json()
    ok = detail["sample"][0]
    upload(client, "G1", [f"R1,{ok['nameplate_no']},{ok['entry_no']}",
                          "R2,NP-X,"])
    d = find_draft(client, "G1")
    # 用页面 token 下载: 内容与页面待复核行/原因逐行一致
    resp = client.get(f"/api/batches/B1/rekey/drafts/G1/download?token={d['token']}")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["Content-Disposition"]
    body = resp.data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0] == ["新条目号", "铭牌编号", "原条目号", "待复核原因"]
    page = [[p["new_entry_no"], p["nameplate_no"], p["original_entry_no"], p["reason"]]
            for p in d["pending"]]
    assert rows[1:] == page
    # 旧 token 失效(模拟页面打开后清单变化)
    stale = d["token"]
    upload(client, "G2", [f"R9,{ok['nameplate_no']},{ok['entry_no']}"])  # 制造重复引用
    resp = client.get(f"/api/batches/B1/rekey/drafts/G1/download?token={stale}")
    assert resp.status_code == 409
    assert "刷新" in resp.get_json()["error"]
    # 新 token 可下载; 别的稿(G2)自己的 token 始终可用
    g1_new = find_draft(client, "G1")
    assert client.get(
        f"/api/batches/B1/rekey/drafts/G1/download?token={g1_new['token']}").status_code == 200
    g2 = find_draft(client, "G2")
    assert client.get(
        f"/api/batches/B1/rekey/drafts/G2/download?token={g2['token']}").status_code == 200


def test_other_draft_change_keeps_unchanged_draft_download(client):
    open_and_close(client)
    detail = client.get("/api/batches/B1").get_json()
    s0, s1 = detail["sample"][0], detail["sample"][1]
    upload(client, "G1", [f"R1,{s0['nameplate_no']},{s0['entry_no']}"])
    upload(client, "G2", [f"R2,{s1['nameplate_no']},{s1['entry_no']}"])
    g1_token = find_draft(client, "G1")["token"]
    # G2 整份替换但不触碰 G1 引用的原条目 -> G1 清单不变, 旧链接仍可下载
    upload(client, "G2", [f"R2,{s1['nameplate_no']},{s1['entry_no']}", "R3,NP-NEW,"])
    assert find_draft(client, "G1")["token"] == g1_token
    assert client.get(
        f"/api/batches/B1/rekey/drafts/G1/download?token={g1_token}").status_code == 200


def test_html_upload_and_download_flow(client):
    open_and_close(client)
    detail = client.get("/api/batches/B1").get_json()
    ok = detail["sample"][0]
    csv_text = "新条目号,铭牌编号,原条目号\n" + (
        f"R1,{ok['nameplate_no']},{ok['entry_no']}\nR2,NP-X,\n")
    resp = client.post("/batches/B1/rekey/drafts",
                       data={"draft_no": "G1", "draft_text": csv_text},
                       follow_redirects=False)
    assert resp.status_code == 302
    page = client.get("/batches/B1")
    assert page.status_code == 200
    assert "重录核对".encode() in page.data and "稿号 G1".encode() in page.data
    d = find_draft(client, "G1")
    # 有效 token 下载
    resp = client.get(f"/batches/B1/rekey/drafts/G1/download?token={d['token']}")
    assert resp.status_code == 200 and "attachment" in resp.headers["Content-Disposition"]
    # 失效 token: 302 回详情页并闪提示
    resp = client.get("/batches/B1/rekey/drafts/G1/download?token=deadbeef",
                      follow_redirects=False)
    assert resp.status_code == 302
    followed = client.get(resp.headers["Location"], follow_redirects=True)
    assert "暂停下载".encode() in followed.data and "刷新".encode() in followed.data


def test_file_upload_takes_precedence_over_textarea(client):
    open_and_close(client)
    detail = client.get("/api/batches/B1").get_json()
    ok = detail["sample"][0]
    import io as _io
    payload = {
        "draft_no": "G1",
        "draft_text": "R-IGNORED,NP-X,",
        "draft_file": (_io.BytesIO(
            ("新条目号,铭牌编号,原条目号\n"
             f"R-FILE,{ok['nameplate_no']},{ok['entry_no']}\n").encode("utf-8-sig")),
            "g1.csv"),
    }
    resp = client.post("/batches/B1/rekey/drafts", data=payload,
                       content_type="multipart/form-data")
    assert resp.status_code == 302
    d = find_draft(client, "G1")
    assert {r["new_entry_no"] for r in d["inherited"]} == {"R-FILE"}
    assert d["row_count"] == 1
