"""Flask 应用: 铭牌编号入仓前抽查核对。"""

import json
import os
from urllib.parse import quote

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, url_for, Response)

import core
import db as dbmod
import rekey
from stats import min_exceeding_errors, pass_threshold


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-key"),
        DB_PATH=os.environ.get("DB_PATH",
                               os.path.join(app.instance_path, "nameplate.db")),
    )
    if config:
        app.config.update(config)
    os.makedirs(app.instance_path, exist_ok=True)
    dbmod.init_app(app)
    with app.app_context():
        dbmod.init_db()

    # ------------------------------------------------------------ 视图模型
    def batch_json(row):
        d = dict(row)
        d["checkpoints"] = json.loads(d["checkpoints"])
        d["status_label"] = core.STATUS_LABELS[d["status"]]
        d["assumed_errors"] = min_exceeding_errors(d["size"], d["limit_pct"])
        d["threshold"] = pass_threshold(len(d["checkpoints"]))
        return d

    def detail_model(batch_no):
        conn = dbmod.get_db()
        row = core.get_batch_by_no(conn, batch_no)
        if row is None:
            return None
        batch = batch_json(row)
        sample = [dict(r) for r in core.sample_items(conn, row["id"])]
        for it in sample:
            it["result_label"] = core.RESULT_LABELS.get(it["result"] or "", "")
        judgments = [dict(r) for r in core.judgments_of(conn, row["id"])]
        progress = core.checkpoint_progress(conn, row["id"], batch["checkpoints"])
        judged_idx = {j["checkpoint_idx"] for j in judgments}
        for p in progress:
            p["judged"] = p["idx"] in judged_idx
        pending = []
        if batch["status"] == "open" and batch["next_checkpoint"] <= len(batch["checkpoints"]):
            upto = batch["checkpoints"][batch["next_checkpoint"] - 1]
            pending = [it["entry_no"] for it in sample
                       if it["position"] <= upto and it["result"] is None]
        rekey_drafts = rekey.all_review_models(conn, row) if batch["status"] != "open" else []
        return {"batch": batch, "sample": sample, "judgments": judgments,
                "progress": progress, "pending_entries": pending,
                "rekey_drafts": rekey_drafts}

    def judgment_message(j, total):
        base = (f"检查点 {j['checkpoint_idx']}(累计抽查 {j['n']} 条): 错录 {j['errors']} 条, "
                f"P={j['prob']:.4g}, 阈值 {j['threshold']:.4g} —— ")
        if j["passed"]:
            return base + "通过, 批次放行, 结论已封存。"
        if j["checkpoint_idx"] == total:
            return base + "未通过, 全部检查点核完, 批次标为待全检。"
        return base + "未通过, 继续下一检查点。"

    # ------------------------------------------------------------ 网页
    @app.get("/")
    def index():
        return redirect(url_for("list_batches"))

    @app.get("/batches")
    def list_batches():
        conn = dbmod.get_db()
        batches = [batch_json(r) for r in core.list_batches(conn)]
        return render_template("batches.html", batches=batches)

    @app.get("/batches/new")
    def new_batch():
        return render_template("new_batch.html", form={})

    @app.post("/batches")
    def create_batch_html():
        form = request.form
        file = request.files.get("list_file")
        text = file.read().decode("utf-8") if file and file.filename else form.get("items_text", "")
        try:
            limit_pct = int(form.get("limit_pct", ""))
        except ValueError:
            limit_pct = -1
        try:
            checkpoints = core.parse_checkpoints(form.get("checkpoints", ""))
            items = core.parse_items_text(text)
            batch, created, notice = core.create_batch(
                dbmod.get_db(), form.get("batch_no"), limit_pct, checkpoints, items,
                seed=form.get("seed"))
        except core.DomainError as exc:
            flash(str(exc), "error")
            return render_template("new_batch.html", form=form), 400
        flash(notice, "ok" if created else "warn")
        return redirect(url_for("batch_detail", batch_no=batch["batch_no"]))

    @app.get("/batches/<batch_no>")
    def batch_detail(batch_no):
        model = detail_model(batch_no)
        if model is None:
            abort(404)
        return render_template("batch_detail.html", **model)

    @app.post("/batches/<batch_no>/entry")
    def entry_html(batch_no):
        conn = dbmod.get_db()
        batch = core.get_batch_by_no(conn, batch_no)
        if batch is None:
            abort(404)
        try:
            item, made = core.record_result(
                conn, batch["id"], request.form.get("entry_no"),
                request.form.get("result"))
        except core.DomainError as exc:
            flash(str(exc), "error")
            return redirect(url_for("batch_detail", batch_no=batch_no))
        flash(f"已录入: 条目 {item['entry_no']}(抽样第 {item['position']} 条) "
              f"核为{core.RESULT_LABELS[item['result']]}。", "ok")
        for j in made:
            flash(judgment_message(j, len(json.loads(batch["checkpoints"]))), "warn")
        return redirect(url_for("batch_detail", batch_no=batch_no))

    # ------------------------------------------------------------ 重录核对网页
    def _uploaded_text():
        file = request.files.get("draft_file")
        if file and file.filename:
            return file.read().decode("utf-8-sig")
        return request.form.get("draft_text", "")

    @app.post("/batches/<batch_no>/rekey/drafts")
    def upload_rekey_draft_html(batch_no):
        conn = dbmod.get_db()
        batch = core.get_batch_by_no(conn, batch_no)
        if batch is None:
            abort(404)
        try:
            draft, replaced = rekey.upload_draft(
                conn, batch, request.form.get("draft_no"), _uploaded_text())
        except core.DomainError as exc:
            flash(str(exc), "error")
            return redirect(url_for("batch_detail", batch_no=batch_no))
        pending, _inherited = rekey.draft_review(conn, batch["id"], draft)
        verb = "整份替换" if replaced else "上传"
        flash(f"已{verb}重录稿「{draft['draft_no']}」(第 {draft['version']} 版, "
              f"{draft['row_count']} 行); 现行待复核 {len(pending)} 行, "
              f"已按全部现行稿重新判定。", "ok")
        return redirect(url_for("batch_detail", batch_no=batch_no,
                                anchor=f"draft-{draft['draft_no']}"))

    def _review_or_none(batch_no, draft_no):
        conn = dbmod.get_db()
        batch = core.get_batch_by_no(conn, batch_no)
        if batch is None:
            return None
        draft = rekey.get_draft(conn, batch["id"], draft_no)
        if draft is None:
            return None
        return batch, draft, rekey.review_model(conn, batch, draft)

    @app.get("/batches/<batch_no>/rekey/drafts/<path:draft_no>/download")
    def download_rekey_pending_html(batch_no, draft_no):
        found = _review_or_none(batch_no, draft_no)
        if found is None:
            abort(404)
        _batch, draft, model = found
        token = request.args.get("token", "")
        if token != model["token"]:
            flash(f"稿「{draft_no}」的待复核清单在页面打开后已有变化, 已暂停下载, "
                  "请刷新页面核对后再下载。", "error")
            return redirect(url_for("batch_detail", batch_no=batch_no,
                                    anchor=f"draft-{draft_no}"))
        return _pending_csv_response(batch_no, draft_no, model)

    def _pending_csv_response(batch_no, draft_no, model):
        # utf-8-sig 便于 Excel 直接打开; 中文文件名按 RFC 5987 编码
        filename = f"{batch_no}_{draft_no}_待复核清单.csv"
        resp = Response("﻿" + rekey.pending_csv_text(model["pending"]),
                        mimetype="text/csv; charset=utf-8")
        resp.headers["Content-Disposition"] = (
            "attachment; filename=pending-review.csv; "
            f"filename*=UTF-8''{quote(filename)}")
        return resp

    # ------------------------------------------------------------ JSON API
    @app.post("/api/batches")
    def api_create_batch():
        data = request.get_json(silent=True) or {}
        try:
            limit_pct = data.get("limit_pct")
            checkpoints = core.parse_checkpoints(data.get("checkpoints"))
            items = core.normalize_items(data.get("items"))
            batch, created, notice = core.create_batch(
                dbmod.get_db(), data.get("batch_no"), limit_pct, checkpoints, items,
                seed=data.get("seed"))
        except core.DomainError as exc:
            return jsonify({"error": str(exc)}), exc.status
        return jsonify({"created": created, "notice": notice,
                        "batch": batch_json(batch)}), 201 if created else 200

    @app.get("/api/batches")
    def api_list_batches():
        conn = dbmod.get_db()
        return jsonify({"batches": [batch_json(r) for r in core.list_batches(conn)]})

    @app.get("/api/batches/<batch_no>")
    def api_batch_detail(batch_no):
        model = detail_model(batch_no)
        if model is None:
            return jsonify({"error": "批次不存在"}), 404
        return jsonify(model)

    @app.post("/api/batches/<batch_no>/results")
    def api_record_result(batch_no):
        conn = dbmod.get_db()
        batch = core.get_batch_by_no(conn, batch_no)
        if batch is None:
            return jsonify({"error": "批次不存在"}), 404
        data = request.get_json(silent=True) or {}
        try:
            item, made = core.record_result(
                conn, batch["id"], data.get("entry_no"), data.get("result"))
        except core.DomainError as exc:
            return jsonify({"error": str(exc)}), exc.status
        fresh = core.get_batch(conn, batch["id"])
        return jsonify({
            "ok": True,
            "entry_no": item["entry_no"],
            "position": item["position"],
            "result": item["result"],
            "judgments": made,
            "status": fresh["status"],
            "status_label": core.STATUS_LABELS[fresh["status"]],
        })

    # ------------------------------------------------------------ 重录核对 API
    @app.post("/api/batches/<batch_no>/rekey/drafts")
    def api_upload_rekey_draft(batch_no):
        conn = dbmod.get_db()
        batch = core.get_batch_by_no(conn, batch_no)
        if batch is None:
            return jsonify({"error": "批次不存在"}), 404
        data = request.get_json(silent=True) or {}
        try:
            draft, replaced = rekey.upload_draft(
                conn, batch, data.get("draft_no"), data.get("csv_text"))
        except core.DomainError as exc:
            return jsonify({"error": str(exc)}), exc.status
        model = rekey.review_model(conn, batch, draft)
        return jsonify({"ok": True, "replaced": replaced, "draft": model}), \
            200 if replaced else 201

    @app.get("/api/batches/<batch_no>/rekey/drafts")
    def api_list_rekey_drafts(batch_no):
        conn = dbmod.get_db()
        batch = core.get_batch_by_no(conn, batch_no)
        if batch is None:
            return jsonify({"error": "批次不存在"}), 404
        return jsonify({"drafts": rekey.all_review_models(conn, batch)})

    @app.get("/api/batches/<batch_no>/rekey/drafts/<path:draft_no>")
    def api_rekey_draft(batch_no, draft_no):
        conn = dbmod.get_db()
        batch = core.get_batch_by_no(conn, batch_no)
        if batch is None:
            return jsonify({"error": "批次不存在"}), 404
        draft = rekey.get_draft(conn, batch["id"], draft_no)
        if draft is None:
            return jsonify({"error": "重录稿不存在"}), 404
        return jsonify(rekey.review_model(conn, batch, draft))

    @app.get("/api/batches/<batch_no>/rekey/drafts/<path:draft_no>/download")
    def api_download_rekey_pending(batch_no, draft_no):
        conn = dbmod.get_db()
        batch = core.get_batch_by_no(conn, batch_no)
        if batch is None:
            return jsonify({"error": "批次不存在"}), 404
        draft = rekey.get_draft(conn, batch["id"], draft_no)
        if draft is None:
            return jsonify({"error": "重录稿不存在"}), 404
        model = rekey.review_model(conn, batch, draft)
        if request.args.get("token", "") != model["token"]:
            return jsonify({
                "error": "待复核清单已变化, 请刷新页面获取最新清单与 token",
                "current_token": model["token"]}), 409
        return _pending_csv_response(batch_no, draft_no, model)

    @app.errorhandler(404)
    def not_found(_exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": "资源不存在"}), 404
        return render_template("404.html"), 404

    return app
