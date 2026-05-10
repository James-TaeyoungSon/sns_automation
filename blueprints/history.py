# -*- coding: utf-8 -*-
from flask import Blueprint, render_template

from database import db_conn

bp = Blueprint("history", __name__)


@bp.route("/history")
def history_page():
    with db_conn() as con:
        rows = con.execute(
            """SELECT p.*, a.title, a.url AS article_url, a.source
               FROM posts p
               JOIN articles a ON a.id = p.article_id
               ORDER BY p.published_at DESC
               LIMIT 100"""
        ).fetchall()
    posts = [dict(r) for r in rows]
    return render_template("history.html", posts=posts)
