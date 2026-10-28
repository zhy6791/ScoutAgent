"""SQLite 持久化层（真实落盘，切页面/刷新/重启都在）。

存储：调研任务 / 报告 / 证据溯源 / 竞品监控订阅 / 专家工作量。
所有读写都走这里，绝不再用内存 dict 当真相源。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _resolve_db_path() -> Path:
    """选择数据库落盘位置。

    本地：app/data/verda.db（持久）。
    Vercel：文件系统只读，唯一可写目录是 /tmp（函数生命周期内有效，
    用户已接受刷新/重启后不持久化）。可用 VERDA_DB_PATH 覆盖。
    """
    override = os.environ.get("VERDA_DB_PATH")
    if override:
        return Path(override)
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return Path("/tmp/verda.db")
    return Path(__file__).resolve().parent.parent / "data" / "verda.db"


_DB_PATH = _resolve_db_path()
# 写锁：SQLite 单写多读，写操作串行化以避免 "database is locked"。
_LOCK = threading.RLock()
# 线程本地连接：FastAPI 同步端点跑在线程池里，编排阶段又用 asyncio.to_thread
# 派生大量工作线程。绝不能多线程共用同一个 sqlite3.Connection（会触发
# "Recursive use of cursors"/句柄竞争，表现为后端卡死、读到空数据）。
# 每个线程持有自己的连接 + WAL 模式 + busy_timeout，实现真正的并发安全。
_LOCAL = threading.local()
_CONNECTIONS = set()
_CONNECTIONS_LOCK = threading.Lock()
_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _utc_now() -> str:
    """监控子系统统一使用 UTC，避免订阅时区影响持久化和租约比较。"""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        return conn
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL：读写并发不互相阻塞（读不挡写、写不挡读）；busy_timeout 让并发写排队而非报错。
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except sqlite3.Error:
        pass
    _ensure_schema(conn)
    _LOCAL.conn = conn
    with _CONNECTIONS_LOCK:
        _CONNECTIONS.add(conn)
    return conn


def close_thread_connection() -> None:
    """关闭当前线程的 SQLite 连接，供短生命周期线程池任务收尾。"""
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        return
    try:
        conn.close()
    finally:
        with _CONNECTIONS_LOCK:
            _CONNECTIONS.discard(conn)
        delattr(_LOCAL, "conn")


def close_all_connections() -> None:
    """关闭当前进程已登记的连接，主要用于应用停机和测试收尾。"""
    with _CONNECTIONS_LOCK:
        connections = tuple(_CONNECTIONS)
        _CONNECTIONS.clear()
    for conn in connections:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if hasattr(_LOCAL, "conn"):
        delattr(_LOCAL, "conn")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """整库 schema 只需初始化一次；后续线程连接复用已建好的表。"""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        _init_schema(conn)
        _SCHEMA_READY = True


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            query TEXT,
            clarifications TEXT,
            status TEXT,
            created_at TEXT,
            report_id TEXT
        );
        CREATE TABLE IF NOT EXISTS reports (
            report_id TEXT PRIMARY KEY,
            task_id TEXT,
            title TEXT,
            subtitle TEXT,
            query TEXT,
            brands TEXT,
            experts TEXT,
            cover_image TEXT,
            data TEXT,
            evidence_count INTEGER,
            claim_count INTEGER,
            high_conf_count INTEGER,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS evidences (
            evidence_id TEXT PRIMARY KEY,
            report_id TEXT,
            source_url TEXT,
            source_type TEXT,
            domain TEXT,
            title TEXT,
            excerpt TEXT,
            credibility REAL,
            collected_by TEXT,
            brand TEXT,
            captured_at TEXT
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            sub_id TEXT PRIMARY KEY,
            query TEXT,
            brands TEXT,
            created_at TEXT,
            last_run_at TEXT,
            last_report_id TEXT,
            run_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS expert_stats (
            expert_id TEXT PRIMARY KEY,
            missions INTEGER DEFAULT 0,
            claims_authored INTEGER DEFAULT 0,
            evidence_collected INTEGER DEFAULT 0,
            last_active TEXT
        );
        CREATE TABLE IF NOT EXISTS traces (
            span_id TEXT PRIMARY KEY,
            task_id TEXT,
            report_id TEXT,
            seq INTEGER,
            agent_id TEXT,
            stage TEXT,
            purpose TEXT,
            model TEXT,
            prompt TEXT,
            response TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            latency_ms INTEGER,
            decision TEXT,
            evidence_ids TEXT,
            ts TEXT
        );
        CREATE TABLE IF NOT EXISTS report_feedback (
            report_id TEXT PRIMARY KEY,
            edited_blocks INTEGER,
            total_blocks INTEGER,
            data TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    _record_migration(conn, 1, "baseline")
    _apply_migration(conn, 2, "subscription_monitor_columns", _migration_v2)
    _apply_migration(conn, 3, "subscription_monitor_tables", _migration_v3)
    _apply_migration(conn, 4, "subscription_monitor_indexes", _migration_v4)
    _apply_migration(conn, 5, "monitor_research_links", _migration_v5)
    _apply_migration(conn, 6, "monitor_observability", _migration_v6)


def _record_migration(conn: sqlite3.Connection, version: int, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
        (version, name, _utc_now()),
    )
    conn.commit()


def _apply_migration(conn: sqlite3.Connection, version: int, name: str, migration) -> None:
    found = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
    ).fetchone()
    if found:
        return
    migration(conn)
    conn.execute(
        "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
        (version, name, _utc_now()),
    )
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migration_v2(conn: sqlite3.Connection) -> None:
    for column, definition in (
        ("enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("schedule_type", "TEXT NOT NULL DEFAULT 'interval'"),
        ("schedule_config", "TEXT NOT NULL DEFAULT '{}'"),
        ("timezone", "TEXT NOT NULL DEFAULT 'Asia/Shanghai'"),
        ("next_run_at", "TEXT NOT NULL DEFAULT ''"),
        ("last_checked_at", "TEXT NOT NULL DEFAULT ''"),
        ("last_success_at", "TEXT NOT NULL DEFAULT ''"),
        ("last_status", "TEXT NOT NULL DEFAULT 'needs_configuration'"),
        ("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
        ("monitor_error", "TEXT NOT NULL DEFAULT ''"),
        ("lease_owner", "TEXT NOT NULL DEFAULT ''"),
        ("lease_expires_at", "TEXT NOT NULL DEFAULT ''"),
        ("updated_at", "TEXT NOT NULL DEFAULT ''"),
    ):
        _ensure_column(conn, "subscriptions", column, definition)
    conn.execute(
        "UPDATE subscriptions SET updated_at=CASE WHEN created_at!='' THEN created_at ELSE ? END "
        "WHERE updated_at=''",
        (_utc_now(),),
    )


def _migration_v3(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS subscription_targets (
            target_id TEXT PRIMARY KEY,
            sub_id TEXT NOT NULL,
            brand TEXT NOT NULL,
            target_type TEXT NOT NULL,
            url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_snapshot_id TEXT NOT NULL DEFAULT '',
            last_hash TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT NOT NULL DEFAULT '',
            failure_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(sub_id, url),
            FOREIGN KEY(sub_id) REFERENCES subscriptions(sub_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS monitor_runs (
            run_id TEXT PRIMARY KEY,
            sub_id TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            scheduled_at TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            target_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            changed_count INTEGER NOT NULL DEFAULT 0,
            search_calls INTEGER NOT NULL DEFAULT 0,
            llm_tokens INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(sub_id) REFERENCES subscriptions(sub_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS page_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            previous_snapshot_id TEXT,
            source_url TEXT NOT NULL,
            final_url TEXT NOT NULL DEFAULT '',
            http_status INTEGER NOT NULL DEFAULT 0,
            content_hash TEXT NOT NULL DEFAULT '',
            content_length INTEGER NOT NULL DEFAULT 0,
            content_excerpt TEXT NOT NULL DEFAULT '',
            snapshot_path TEXT NOT NULL DEFAULT '',
            etag TEXT NOT NULL DEFAULT '',
            last_modified TEXT NOT NULL DEFAULT '',
            captured_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES monitor_runs(run_id) ON DELETE CASCADE,
            FOREIGN KEY(target_id) REFERENCES subscription_targets(target_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS competitor_changes (
            change_id TEXT PRIMARY KEY,
            sub_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            previous_snapshot_id TEXT NOT NULL,
            current_snapshot_id TEXT NOT NULL,
            change_type TEXT NOT NULL DEFAULT 'unknown',
            severity TEXT NOT NULL DEFAULT 'info',
            material INTEGER NOT NULL DEFAULT 0,
            change_ratio REAL NOT NULL DEFAULT 0,
            diff_summary TEXT NOT NULL DEFAULT '',
            before_excerpt TEXT NOT NULL DEFAULT '',
            after_excerpt TEXT NOT NULL DEFAULT '',
            llm_status TEXT NOT NULL DEFAULT 'skipped',
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL,
            UNIQUE(target_id, previous_snapshot_id, current_snapshot_id),
            FOREIGN KEY(sub_id) REFERENCES subscriptions(sub_id) ON DELETE CASCADE,
            FOREIGN KEY(run_id) REFERENCES monitor_runs(run_id) ON DELETE CASCADE,
            FOREIGN KEY(target_id) REFERENCES subscription_targets(target_id) ON DELETE CASCADE,
            FOREIGN KEY(previous_snapshot_id) REFERENCES page_snapshots(snapshot_id),
            FOREIGN KEY(current_snapshot_id) REFERENCES page_snapshots(snapshot_id)
        );
        """
    )


def _migration_v4(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_subscriptions_due
            ON subscriptions(enabled, next_run_at);
        CREATE INDEX IF NOT EXISTS idx_subscription_targets_active
            ON subscription_targets(sub_id, enabled);
        CREATE INDEX IF NOT EXISTS idx_monitor_runs_subscription
            ON monitor_runs(sub_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_page_snapshots_target
            ON page_snapshots(target_id, captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_competitor_changes_subscription
            ON competitor_changes(sub_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_competitor_changes_inbox
            ON competitor_changes(status, severity);
        """
    )


def _migration_v5(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "tasks", "source_change_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "tasks", "source_snapshot_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "reports", "source_change_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "reports", "source_snapshot_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "competitor_changes", "research_task_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "competitor_changes", "research_report_id", "TEXT NOT NULL DEFAULT ''")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_seed_evidences (
            task_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            change_id TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(task_id, evidence_id),
            FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_task_seed_evidences_task
            ON task_seed_evidences(task_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_competitor_changes_research
            ON competitor_changes(research_task_id, research_report_id);
        """
    )


def _migration_v6(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "monitor_runs", "not_modified_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "monitor_runs", "minor_change_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "monitor_runs", "llm_failure_count", "INTEGER NOT NULL DEFAULT 0")


def schema_version() -> int:
    row = _connect().execute("SELECT COALESCE(MAX(version),0) version FROM schema_migrations").fetchone()
    return int(row["version"] if row else 0)


# ── 任务 ────────────────────────────────────────────────
def save_task(task_id: str, query: str, clarifications: Dict[str, Any]) -> None:
    with _LOCK:
        c = _connect()
        c.execute(
            "INSERT OR REPLACE INTO tasks(task_id,query,clarifications,status,created_at,report_id)"
            " VALUES(?,?,?,?,?,COALESCE((SELECT report_id FROM tasks WHERE task_id=?),NULL))",
            (task_id, query, json.dumps(clarifications, ensure_ascii=False), "created", _now(), task_id),
        )
        c.commit()


def update_task_clarify(task_id: str, clarifications: Dict[str, Any]) -> None:
    with _LOCK:
        c = _connect()
        c.execute(
            "UPDATE tasks SET clarifications=?, status='clarified' WHERE task_id=?",
            (json.dumps(clarifications, ensure_ascii=False), task_id),
        )
        c.commit()


def update_task_context(task_id: str, clarifications: Dict[str, Any]) -> None:
    """更新任务预填上下文，但不跳过用户澄清状态。"""
    with _LOCK:
        c = _connect()
        c.execute(
            "UPDATE tasks SET clarifications=? WHERE task_id=?",
            (json.dumps(clarifications, ensure_ascii=False), task_id),
        )
        c.commit()


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    c = _connect()
    row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["clarifications"] = json.loads(d.get("clarifications") or "{}")
    return d


def mark_task_done(task_id: str, report_id: str) -> None:
    with _LOCK:
        c = _connect()
        c.execute(
            "UPDATE tasks SET status='done', report_id=? WHERE task_id=?",
            (report_id, task_id),
        )
        c.commit()


def set_task_monitor_source(task_id: str, change_id: str, snapshot_id: str) -> None:
    with _LOCK:
        c = _connect()
        c.execute(
            "UPDATE tasks SET source_change_id=?,source_snapshot_id=? WHERE task_id=?",
            (change_id, snapshot_id, task_id),
        )
        c.execute(
            "UPDATE competitor_changes SET research_task_id=? WHERE change_id=?",
            (task_id, change_id),
        )
        c.commit()


def save_task_seed_evidence(
    task_id: str,
    evidence_id: str,
    snapshot_id: str,
    change_id: str,
    data: Dict[str, Any],
) -> None:
    with _LOCK:
        c = _connect()
        c.execute(
            "INSERT OR REPLACE INTO task_seed_evidences("
            "task_id,evidence_id,snapshot_id,change_id,data,created_at) VALUES(?,?,?,?,?,?)",
            (task_id, evidence_id, snapshot_id, change_id,
             json.dumps(data, ensure_ascii=False), _utc_now()),
        )
        c.commit()


def list_task_seed_evidences(task_id: str) -> List[Dict[str, Any]]:
    rows = _connect().execute(
        "SELECT evidence_id,snapshot_id,change_id,data,created_at "
        "FROM task_seed_evidences WHERE task_id=? ORDER BY created_at",
        (task_id,),
    ).fetchall()
    output = []
    for row in rows:
        item = _loads_json(row["data"], {})
        item.update({
            "evidence_id": row["evidence_id"],
            "snapshot_id": row["snapshot_id"],
            "change_id": row["change_id"],
            "seed_created_at": row["created_at"],
        })
        output.append(item)
    return output


def link_monitor_report_for_task(task_id: str, report_id: str) -> None:
    task = get_task(task_id)
    if not task or not task.get("source_change_id"):
        return
    with _LOCK:
        c = _connect()
        c.execute(
            "UPDATE reports SET source_change_id=?,source_snapshot_id=? WHERE report_id=?",
            (task.get("source_change_id", ""), task.get("source_snapshot_id", ""), report_id),
        )
        c.execute(
            "UPDATE competitor_changes SET research_report_id=? WHERE change_id=?",
            (report_id, task["source_change_id"]),
        )
        c.commit()


# ── 报告 + 证据 ─────────────────────────────────────────
def save_report(report: Dict[str, Any], task_id: str = "") -> None:
    evidence = report.get("evidence", [])
    claims = report.get("claims", [])
    high = sum(1 for c in claims if c.get("confidence") == "high")
    with _LOCK:
        c = _connect()
        c.execute(
            "INSERT OR REPLACE INTO reports(report_id,task_id,title,subtitle,query,brands,experts,"
            "cover_image,data,evidence_count,claim_count,high_conf_count,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                report["id"], task_id, report.get("title", ""), report.get("subtitle", ""),
                report.get("query", ""), json.dumps(report.get("brands", []), ensure_ascii=False),
                json.dumps(report.get("experts", []), ensure_ascii=False),
                report.get("cover_image", ""), json.dumps(report, ensure_ascii=False),
                len(evidence), len(claims), high, report.get("created_at", _now()),
            ),
        )
        # 证据溯源单独入库，供全局证据库检索
        for ev in evidence:
            c.execute(
                "INSERT OR REPLACE INTO evidences(evidence_id,report_id,source_url,source_type,"
                "domain,title,excerpt,credibility,collected_by,brand,captured_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ev.get("evidence_id"), report["id"], ev.get("source_url", ""),
                    ev.get("source_type", ""), ev.get("domain", ""), ev.get("title", ""),
                    ev.get("excerpt", "")[:500], ev.get("credibility", 0.0),
                    ev.get("collected_by", ""), ev.get("brand", ""), ev.get("captured_at", _now()),
                ),
            )
        c.commit()


def get_report(report_id: str) -> Optional[Dict[str, Any]]:
    c = _connect()
    row = c.execute("SELECT data FROM reports WHERE report_id=?", (report_id,)).fetchone()
    if not row:
        return None
    return json.loads(row["data"])


def list_reports() -> List[Dict[str, Any]]:
    """报告卡片列表（不含全文 data，省带宽）。"""
    c = _connect()
    rows = c.execute(
        "SELECT report_id,title,subtitle,query,brands,experts,cover_image,"
        "evidence_count,claim_count,high_conf_count,created_at FROM reports ORDER BY created_at DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = d["report_id"]
        d["brands"] = json.loads(d.get("brands") or "[]")
        d["experts"] = json.loads(d.get("experts") or "[]")
        out.append(d)
    return out


# ── 全局证据溯源库 ──────────────────────────────────────
def query_evidences(
    brand: Optional[str] = None,
    source_type: Optional[str] = None,
    min_cred: float = 0.0,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    c = _connect()
    sql = ("SELECT e.*,"
           "COALESCE((SELECT snapshot_id FROM task_seed_evidences tse "
           "WHERE tse.evidence_id=e.evidence_id LIMIT 1),'') snapshot_id,"
           "COALESCE((SELECT change_id FROM task_seed_evidences tse "
           "WHERE tse.evidence_id=e.evidence_id LIMIT 1),'') change_id "
           "FROM evidences e WHERE e.credibility>=?")
    args: List[Any] = [min_cred]
    if brand:
        sql += " AND e.brand=?"
        args.append(brand)
    if source_type:
        sql += " AND e.source_type=?"
        args.append(source_type)
    sql += " ORDER BY e.credibility DESC, e.captured_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def evidence_facets() -> Dict[str, Any]:
    """证据库聚合：平台分布 / 品牌分布 / 总量。"""
    c = _connect()
    total = c.execute("SELECT COUNT(*) n FROM evidences").fetchone()["n"]
    by_type = {
        r["source_type"]: r["n"]
        for r in c.execute(
            "SELECT source_type, COUNT(*) n FROM evidences GROUP BY source_type"
        ).fetchall()
    }
    by_brand = {
        r["brand"]: r["n"]
        for r in c.execute(
            "SELECT brand, COUNT(*) n FROM evidences WHERE brand!='' GROUP BY brand ORDER BY n DESC LIMIT 12"
        ).fetchall()
    }
    return {"total": total, "by_type": by_type, "by_brand": by_brand}


# ── 调研统计（真实仪表盘）─────────────────────────────────
def dashboard_stats() -> Dict[str, Any]:
    c = _connect()
    reports = c.execute("SELECT COUNT(*) n FROM reports").fetchone()["n"]
    ev_total = c.execute("SELECT COUNT(*) n FROM evidences").fetchone()["n"]
    claim_total = c.execute("SELECT COALESCE(SUM(claim_count),0) n FROM reports").fetchone()["n"]
    high_total = c.execute("SELECT COALESCE(SUM(high_conf_count),0) n FROM reports").fetchone()["n"]
    avg_ev = round(ev_total / reports, 1) if reports else 0
    # 真实事实准确率 = 高置信结论占比
    fact_rate = round(high_total / claim_total * 100) if claim_total else 0
    facets = evidence_facets()
    intel = intel_overview()
    return {
        "reports": reports,
        "evidence_total": ev_total,
        "claim_total": claim_total,
        "high_conf_total": high_total,
        "avg_evidence_per_report": avg_ev,
        "fact_accuracy": fact_rate,
        "platform_distribution": facets["by_type"],
        "brand_distribution": facets["by_brand"],
        # 业务闭环聚合（真实，来自各报告 metrics）
        "minutes_saved": intel["minutes_saved"],
        "avg_efficiency": intel["avg_efficiency"],
        "avg_coverage": intel["avg_coverage"],
        "total_tokens": intel["total_tokens"],
        "research_cards": intel["cards"],
    }


def intel_overview() -> Dict[str, Any]:
    """跨报告聚合真实业务指标 + 每次调研的概览卡（供情报中心）。

    从每份报告存储的 data.metrics 里抽取效率/覆盖/耗时/token，聚合出
    「累计节省人力（分钟）」「平均效率倍数」等可向评委解释的真实数字。
    """
    c = _connect()
    rows = c.execute(
        "SELECT report_id,title,query,brands,evidence_count,claim_count,"
        "high_conf_count,created_at,data FROM reports ORDER BY created_at DESC LIMIT 60"
    ).fetchall()
    cards: List[Dict[str, Any]] = []
    minutes_saved = 0.0
    eff_list: List[float] = []
    cov_list: List[float] = []
    total_tokens = 0
    for r in rows:
        try:
            data = json.loads(r["data"]) if r["data"] else {}
        except Exception:
            data = {}
        m = data.get("metrics") or {}
        eff = (m.get("efficiency") or {})
        cov = (m.get("coverage") or {})
        manual_min = float(eff.get("manual_estimate_minutes") or 0)
        elapsed_min = float(eff.get("elapsed_minutes") or 0)
        saved = max(0.0, manual_min - elapsed_min)
        minutes_saved += saved
        if eff.get("efficiency_multiple"):
            eff_list.append(float(eff["efficiency_multiple"]))
        if cov.get("coverage_multiple"):
            cov_list.append(float(cov["coverage_multiple"]))
        total_tokens += int(eff.get("tokens_used") or 0)
        cards.append({
            "id": r["report_id"],
            "title": r["title"],
            "query": r["query"],
            "brands": json.loads(r["brands"] or "[]"),
            "evidence_count": r["evidence_count"],
            "claim_count": r["claim_count"],
            "high_conf_count": r["high_conf_count"],
            "created_at": r["created_at"],
            "efficiency_multiple": eff.get("efficiency_multiple"),
            "coverage_multiple": cov.get("coverage_multiple"),
            "elapsed_minutes": eff.get("elapsed_minutes"),
            "minutes_saved": round(saved, 1),
            "tokens_used": eff.get("tokens_used"),
        })
    return {
        "minutes_saved": round(minutes_saved, 1),
        "avg_efficiency": round(sum(eff_list) / len(eff_list), 1) if eff_list else 0,
        "avg_coverage": round(sum(cov_list) / len(cov_list), 1) if cov_list else 0,
        "total_tokens": total_tokens,
        "cards": cards,
    }




# ── 竞品监控订阅 ────────────────────────────────────────
_TARGET_TYPES = {"official", "pricing", "blog", "jobs", "custom"}
_SCHEDULE_TYPES = {"interval", "daily"}
_TRIGGER_TYPES = {"scheduled", "manual", "retry"}
_RUN_STATUSES = {"pending", "running", "succeeded", "partial", "failed"}
_CHANGE_STATUSES = {"new", "read", "ignored"}


def normalize_monitor_url(url: str) -> str:
    """生成用于订阅内去重的稳定 URL；网络安全校验在抓取阶段执行。"""
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("monitor URL must use http or https and include a host")
    if parts.username or parts.password:
        raise ValueError("monitor URL must not contain credentials")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("monitor URL contains an invalid port") from exc
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((scheme, host, path, query, ""))


def _loads_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _subscription_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["brands"] = _loads_json(item.get("brands"), [])
    item["schedule_config"] = _loads_json(item.get("schedule_config"), {})
    item["enabled"] = bool(item.get("enabled"))
    return item


def _target_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["enabled"] = bool(item.get("enabled"))
    return item


def _change_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["material"] = bool(item.get("material"))
    return item


def create_subscription(
    sub_id: str,
    query: str,
    brands: List[str],
    *,
    schedule_type: str = "interval",
    schedule_config: Optional[Dict[str, Any]] = None,
    timezone: str = "Asia/Shanghai",
    next_run_at: str = "",
) -> Dict[str, Any]:
    if schedule_type not in _SCHEDULE_TYPES:
        raise ValueError(f"unsupported schedule_type: {schedule_type}")
    now = _utc_now()
    with _LOCK:
        c = _connect()
        c.execute(
            "INSERT INTO subscriptions("
            "sub_id,query,brands,created_at,last_run_at,last_report_id,run_count,enabled,"
            "schedule_type,schedule_config,timezone,next_run_at,last_checked_at,last_success_at,"
            "last_status,consecutive_failures,monitor_error,lease_owner,lease_expires_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(sub_id) DO UPDATE SET query=excluded.query,brands=excluded.brands,"
            "schedule_type=excluded.schedule_type,schedule_config=excluded.schedule_config,"
            "timezone=excluded.timezone,next_run_at=excluded.next_run_at,updated_at=excluded.updated_at",
            (
                sub_id, query.strip(), json.dumps(brands, ensure_ascii=False), now, "", "", 0, 0,
                schedule_type, json.dumps(schedule_config or {}, ensure_ascii=False), timezone,
                next_run_at, "", "", "needs_configuration", 0, "", "", "", now,
            ),
        )
        c.commit()
    return get_subscription(sub_id) or {}


def list_subscriptions() -> List[Dict[str, Any]]:
    c = _connect()
    rows = c.execute("SELECT * FROM subscriptions ORDER BY created_at DESC").fetchall()
    return [_subscription_dict(row) for row in rows]


def get_subscription(sub_id: str) -> Optional[Dict[str, Any]]:
    c = _connect()
    row = c.execute("SELECT * FROM subscriptions WHERE sub_id=?", (sub_id,)).fetchone()
    if not row:
        return None
    return _subscription_dict(row)


def update_subscription(sub_id: str, **changes: Any) -> Optional[Dict[str, Any]]:
    allowed = {"query", "brands", "schedule_type", "schedule_config", "timezone", "next_run_at"}
    values = {key: value for key, value in changes.items() if key in allowed}
    if not values:
        return get_subscription(sub_id)
    if "schedule_type" in values and values["schedule_type"] not in _SCHEDULE_TYPES:
        raise ValueError(f"unsupported schedule_type: {values['schedule_type']}")
    if "brands" in values:
        values["brands"] = json.dumps(values["brands"] or [], ensure_ascii=False)
    if "schedule_config" in values:
        values["schedule_config"] = json.dumps(values["schedule_config"] or {}, ensure_ascii=False)
    values["updated_at"] = _utc_now()
    assignments = ",".join(f"{key}=?" for key in values)
    with _LOCK:
        c = _connect()
        c.execute(
            f"UPDATE subscriptions SET {assignments} WHERE sub_id=?",
            (*values.values(), sub_id),
        )
        if "brands" in values:
            _refresh_subscription_configuration(c, sub_id)
        c.commit()
    return get_subscription(sub_id)


def set_subscription_enabled(sub_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
    now = _utc_now()
    with _LOCK:
        c = _connect()
        if enabled:
            subscription = c.execute(
                "SELECT brands FROM subscriptions WHERE sub_id=?", (sub_id,)
            ).fetchone()
            if not subscription or not _loads_json(subscription["brands"], []):
                raise ValueError("subscription requires at least one brand")
            count = c.execute(
                "SELECT COUNT(*) count FROM subscription_targets WHERE sub_id=? AND enabled=1",
                (sub_id,),
            ).fetchone()["count"]
            if not count:
                raise ValueError("subscription requires at least one enabled target")
            c.execute(
                "UPDATE subscriptions SET enabled=1,last_status='active',monitor_error='',updated_at=? "
                "WHERE sub_id=?",
                (now, sub_id),
            )
        else:
            c.execute(
                "UPDATE subscriptions SET enabled=0,last_status='paused',lease_owner='',"
                "lease_expires_at='',updated_at=? WHERE sub_id=?",
                (now, sub_id),
            )
        c.commit()
    return get_subscription(sub_id)


def delete_subscription(sub_id: str) -> None:
    with _LOCK:
        c = _connect()
        c.execute("DELETE FROM subscriptions WHERE sub_id=?", (sub_id,))
        c.commit()


def mark_subscription_run(sub_id: str, report_id: str) -> None:
    with _LOCK:
        c = _connect()
        c.execute(
            "UPDATE subscriptions SET last_run_at=?,last_report_id=?,run_count=run_count+1,updated_at=? "
            "WHERE sub_id=?",
            (_utc_now(), report_id, _utc_now(), sub_id),
        )
        c.commit()


def _refresh_subscription_configuration(conn: sqlite3.Connection, sub_id: str) -> None:
    subscription = conn.execute(
        "SELECT brands FROM subscriptions WHERE sub_id=?", (sub_id,)
    ).fetchone()
    has_brands = bool(subscription and _loads_json(subscription["brands"], []))
    count = conn.execute(
        "SELECT COUNT(*) count FROM subscription_targets WHERE sub_id=? AND enabled=1", (sub_id,)
    ).fetchone()["count"]
    if not count or not has_brands:
        conn.execute(
            "UPDATE subscriptions SET enabled=0,last_status='needs_configuration',next_run_at='',"
            "lease_owner='',lease_expires_at='',updated_at=? WHERE sub_id=?",
            (_utc_now(), sub_id),
        )
    else:
        conn.execute(
            "UPDATE subscriptions SET last_status=CASE WHEN enabled=1 THEN 'active' ELSE 'paused' END,"
            "updated_at=? WHERE sub_id=? AND last_status='needs_configuration'",
            (_utc_now(), sub_id),
        )


def create_subscription_target(
    target_id: str,
    sub_id: str,
    brand: str,
    target_type: str,
    url: str,
    *,
    enabled: bool = True,
) -> Dict[str, Any]:
    if target_type not in _TARGET_TYPES:
        raise ValueError(f"unsupported target_type: {target_type}")
    normalized_url = normalize_monitor_url(url)
    now = _utc_now()
    with _LOCK:
        c = _connect()
        try:
            c.execute(
                "INSERT INTO subscription_targets(target_id,sub_id,brand,target_type,url,enabled,"
                "last_snapshot_id,last_hash,last_checked_at,failure_count,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (target_id, sub_id, brand.strip(), target_type, normalized_url, int(enabled),
                 "", "", "", 0, now, now),
            )
        except sqlite3.IntegrityError as exc:
            c.rollback()
            raise ValueError("target already exists or subscription is invalid") from exc
        _refresh_subscription_configuration(c, sub_id)
        c.commit()
    return get_subscription_target(target_id) or {}


def get_subscription_target(target_id: str) -> Optional[Dict[str, Any]]:
    row = _connect().execute(
        "SELECT * FROM subscription_targets WHERE target_id=?", (target_id,)
    ).fetchone()
    return _target_dict(row) if row else None


def list_subscription_targets(sub_id: str, *, include_disabled: bool = True) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM subscription_targets WHERE sub_id=?"
    if not include_disabled:
        sql += " AND enabled=1"
    sql += " ORDER BY brand,target_type,created_at"
    rows = _connect().execute(sql, (sub_id,)).fetchall()
    return [_target_dict(row) for row in rows]


def update_subscription_target(target_id: str, **changes: Any) -> Optional[Dict[str, Any]]:
    allowed = {"brand", "target_type", "url", "enabled", "last_snapshot_id", "last_hash",
               "last_checked_at", "failure_count"}
    values = {key: value for key, value in changes.items() if key in allowed}
    current = get_subscription_target(target_id)
    if not current:
        return None
    if not values:
        return current
    if "target_type" in values and values["target_type"] not in _TARGET_TYPES:
        raise ValueError(f"unsupported target_type: {values['target_type']}")
    if "url" in values:
        values["url"] = normalize_monitor_url(values["url"])
    if "enabled" in values:
        values["enabled"] = int(bool(values["enabled"]))
    values["updated_at"] = _utc_now()
    assignments = ",".join(f"{key}=?" for key in values)
    with _LOCK:
        c = _connect()
        try:
            c.execute(
                f"UPDATE subscription_targets SET {assignments} WHERE target_id=?",
                (*values.values(), target_id),
            )
        except sqlite3.IntegrityError as exc:
            c.rollback()
            raise ValueError("target URL duplicates another target in this subscription") from exc
        _refresh_subscription_configuration(c, current["sub_id"])
        c.commit()
    return get_subscription_target(target_id)


def delete_subscription_target(target_id: str) -> None:
    current = get_subscription_target(target_id)
    if not current:
        return
    with _LOCK:
        c = _connect()
        c.execute("DELETE FROM subscription_targets WHERE target_id=?", (target_id,))
        _refresh_subscription_configuration(c, current["sub_id"])
        c.commit()


def create_monitor_run(
    run_id: str,
    sub_id: str,
    trigger_type: str,
    *,
    scheduled_at: str = "",
    target_count: Optional[int] = None,
) -> Dict[str, Any]:
    if trigger_type not in _TRIGGER_TYPES:
        raise ValueError(f"unsupported trigger_type: {trigger_type}")
    with _LOCK:
        c = _connect()
        if target_count is None:
            target_count = c.execute(
                "SELECT COUNT(*) count FROM subscription_targets WHERE sub_id=? AND enabled=1",
                (sub_id,),
            ).fetchone()["count"]
        c.execute(
            "INSERT INTO monitor_runs(run_id,sub_id,trigger_type,scheduled_at,started_at,finished_at,"
            "status,target_count,success_count,changed_count,search_calls,llm_tokens,duration_ms,error,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, sub_id, trigger_type, scheduled_at, "", "", "pending", target_count,
             0, 0, 0, 0, 0, "", _utc_now()),
        )
        c.commit()
    return get_monitor_run(run_id) or {}


def get_monitor_run(run_id: str) -> Optional[Dict[str, Any]]:
    row = _connect().execute("SELECT * FROM monitor_runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_monitor_runs(sub_id: str, *, limit: int = 100) -> List[Dict[str, Any]]:
    rows = _connect().execute(
        "SELECT * FROM monitor_runs WHERE sub_id=? ORDER BY created_at DESC LIMIT ?",
        (sub_id, max(1, min(limit, 500))),
    ).fetchall()
    return [dict(row) for row in rows]


def update_monitor_run(run_id: str, **changes: Any) -> Optional[Dict[str, Any]]:
    allowed = {"scheduled_at", "started_at", "finished_at", "status", "target_count",
               "success_count", "changed_count", "search_calls", "llm_tokens", "duration_ms", "error",
               "not_modified_count", "minor_change_count", "llm_failure_count"}
    values = {key: value for key, value in changes.items() if key in allowed}
    if "status" in values and values["status"] not in _RUN_STATUSES:
        raise ValueError(f"unsupported monitor run status: {values['status']}")
    if not values:
        return get_monitor_run(run_id)
    assignments = ",".join(f"{key}=?" for key in values)
    with _LOCK:
        c = _connect()
        c.execute(f"UPDATE monitor_runs SET {assignments} WHERE run_id=?", (*values.values(), run_id))
        c.commit()
    return get_monitor_run(run_id)


def create_page_snapshot(
    snapshot_id: str,
    run_id: str,
    target_id: str,
    source_url: str,
    *,
    previous_snapshot_id: str = "",
    final_url: str = "",
    http_status: int = 0,
    content_hash: str = "",
    content_length: int = 0,
    content_excerpt: str = "",
    snapshot_path: str = "",
    etag: str = "",
    last_modified: str = "",
    captured_at: str = "",
) -> Dict[str, Any]:
    captured = captured_at or _utc_now()
    with _LOCK:
        c = _connect()
        c.execute(
            "INSERT INTO page_snapshots(snapshot_id,run_id,target_id,previous_snapshot_id,source_url,"
            "final_url,http_status,content_hash,content_length,content_excerpt,snapshot_path,etag,"
            "last_modified,captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, run_id, target_id, previous_snapshot_id or None, source_url, final_url,
             http_status, content_hash, content_length, content_excerpt, snapshot_path, etag,
             last_modified, captured),
        )
        c.execute(
            "UPDATE subscription_targets SET last_snapshot_id=?,last_hash=?,last_checked_at=?,"
            "failure_count=0,updated_at=? WHERE target_id=?",
            (snapshot_id, content_hash, captured, _utc_now(), target_id),
        )
        c.commit()
    return get_page_snapshot(snapshot_id) or {}


def get_page_snapshot(snapshot_id: str) -> Optional[Dict[str, Any]]:
    row = _connect().execute(
        "SELECT * FROM page_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["previous_snapshot_id"] = item.get("previous_snapshot_id") or ""
    return item


def list_page_snapshots(target_id: str, *, limit: int = 100) -> List[Dict[str, Any]]:
    rows = _connect().execute(
        "SELECT * FROM page_snapshots WHERE target_id=? ORDER BY captured_at DESC LIMIT ?",
        (target_id, max(1, min(limit, 500))),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["previous_snapshot_id"] = item.get("previous_snapshot_id") or ""
        out.append(item)
    return out


def delete_page_snapshot_if_unreferenced(snapshot_id: str) -> Optional[Dict[str, Any]]:
    """删除未被变化事件或调研种子引用的旧快照，并返回其文件元数据。"""
    with _LOCK:
        c = _connect()
        row = c.execute(
            "SELECT ps.* FROM page_snapshots ps "
            "JOIN subscription_targets st ON st.target_id=ps.target_id "
            "WHERE ps.snapshot_id=? AND st.last_snapshot_id!=ps.snapshot_id "
            "AND NOT EXISTS(SELECT 1 FROM competitor_changes cc WHERE "
            "cc.previous_snapshot_id=ps.snapshot_id OR cc.current_snapshot_id=ps.snapshot_id) "
            "AND NOT EXISTS(SELECT 1 FROM task_seed_evidences tse WHERE tse.snapshot_id=ps.snapshot_id)",
            (snapshot_id,),
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE page_snapshots SET previous_snapshot_id=? WHERE previous_snapshot_id=?",
            (row["previous_snapshot_id"], snapshot_id),
        )
        c.execute("DELETE FROM page_snapshots WHERE snapshot_id=?", (snapshot_id,))
        c.commit()
    return dict(row)


def create_competitor_change(
    change_id: str,
    sub_id: str,
    run_id: str,
    target_id: str,
    previous_snapshot_id: str,
    current_snapshot_id: str,
    *,
    change_type: str = "unknown",
    severity: str = "info",
    material: bool = False,
    change_ratio: float = 0.0,
    diff_summary: str = "",
    before_excerpt: str = "",
    after_excerpt: str = "",
    llm_status: str = "skipped",
) -> Dict[str, Any]:
    with _LOCK:
        c = _connect()
        c.execute(
            "INSERT OR IGNORE INTO competitor_changes(change_id,sub_id,run_id,target_id,previous_snapshot_id,"
            "current_snapshot_id,change_type,severity,material,change_ratio,diff_summary,before_excerpt,"
            "after_excerpt,llm_status,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (change_id, sub_id, run_id, target_id, previous_snapshot_id, current_snapshot_id,
             change_type, severity, int(material), change_ratio, diff_summary, before_excerpt,
             after_excerpt, llm_status, "new", _utc_now()),
        )
        c.commit()
    return (
        get_competitor_change(change_id)
        or get_competitor_change_for_snapshots(target_id, previous_snapshot_id, current_snapshot_id)
        or {}
    )


def get_competitor_change(change_id: str) -> Optional[Dict[str, Any]]:
    row = _connect().execute(
        "SELECT * FROM competitor_changes WHERE change_id=?", (change_id,)
    ).fetchone()
    return _change_dict(row) if row else None


def get_competitor_change_for_snapshots(
    target_id: str,
    previous_snapshot_id: str,
    current_snapshot_id: str,
) -> Optional[Dict[str, Any]]:
    row = _connect().execute(
        "SELECT * FROM competitor_changes WHERE target_id=? AND previous_snapshot_id=? "
        "AND current_snapshot_id=?",
        (target_id, previous_snapshot_id, current_snapshot_id),
    ).fetchone()
    return _change_dict(row) if row else None


def increment_monitor_run_metrics(
    run_id: str,
    *,
    llm_tokens: int = 0,
    changed_count: int = 0,
) -> Optional[Dict[str, Any]]:
    with _LOCK:
        c = _connect()
        c.execute(
            "UPDATE monitor_runs SET llm_tokens=llm_tokens+?,changed_count=changed_count+? WHERE run_id=?",
            (max(0, int(llm_tokens)), max(0, int(changed_count)), run_id),
        )
        c.commit()
    return get_monitor_run(run_id)


def claim_due_subscriptions(
    worker_id: str,
    *,
    now: str,
    lease_expires_at: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """原子领取到期订阅；多个进程扫描时，同一订阅最多被一个进程领取。"""
    if not worker_id:
        raise ValueError("worker_id is required")
    claimed: List[Dict[str, Any]] = []
    with _LOCK:
        c = _connect()
        c.execute("BEGIN IMMEDIATE")
        try:
            rows = c.execute(
                "SELECT sub_id FROM subscriptions WHERE enabled=1 AND next_run_at!='' "
                "AND next_run_at<=? AND (lease_expires_at='' OR lease_expires_at<=?) "
                "ORDER BY next_run_at LIMIT ?",
                (now, now, max(1, min(int(limit), 500))),
            ).fetchall()
            for row in rows:
                changed = c.execute(
                    "UPDATE subscriptions SET lease_owner=?,lease_expires_at=?,last_status='claimed',"
                    "updated_at=? WHERE sub_id=? AND enabled=1 "
                    "AND (lease_expires_at='' OR lease_expires_at<=?)",
                    (worker_id, lease_expires_at, now, row["sub_id"], now),
                ).rowcount
                if changed:
                    current = c.execute(
                        "SELECT * FROM subscriptions WHERE sub_id=?", (row["sub_id"],)
                    ).fetchone()
                    if current:
                        claimed.append(_subscription_dict(current))
            c.commit()
        except Exception:
            c.rollback()
            raise
    return claimed


def claim_subscription(
    sub_id: str,
    worker_id: str,
    *,
    now: str,
    lease_expires_at: str,
    require_enabled: bool = True,
) -> Optional[Dict[str, Any]]:
    if not worker_id:
        raise ValueError("worker_id is required")
    enabled_clause = "AND enabled=1" if require_enabled else ""
    with _LOCK:
        c = _connect()
        c.execute("BEGIN IMMEDIATE")
        try:
            changed = c.execute(
                "UPDATE subscriptions SET lease_owner=?,lease_expires_at=?,last_status='claimed',"
                f"updated_at=? WHERE sub_id=? {enabled_clause} "
                "AND (lease_expires_at='' OR lease_expires_at<=?)",
                (worker_id, lease_expires_at, now, sub_id, now),
            ).rowcount
            if not changed:
                c.rollback()
                return None
            row = c.execute("SELECT * FROM subscriptions WHERE sub_id=?", (sub_id,)).fetchone()
            c.commit()
        except Exception:
            c.rollback()
            raise
    return _subscription_dict(row) if row else None


def finish_subscription_monitor(
    sub_id: str,
    worker_id: str,
    *,
    status: str,
    next_run_at: str,
    checked_at: str,
    success: bool,
    error: str = "",
) -> Optional[Dict[str, Any]]:
    with _LOCK:
        c = _connect()
        c.execute(
            "UPDATE subscriptions SET last_checked_at=?,"
            "last_success_at=CASE WHEN ?=1 THEN ? ELSE last_success_at END,"
            "last_status=CASE WHEN enabled=1 THEN ? ELSE last_status END,"
            "consecutive_failures=CASE WHEN ?=1 THEN 0 ELSE consecutive_failures+1 END,"
            "monitor_error=?,next_run_at=CASE WHEN enabled=1 THEN ? ELSE '' END,"
            "run_count=run_count+1,"
            "lease_owner='',lease_expires_at='',updated_at=? "
            "WHERE sub_id=? AND lease_owner=?",
            (checked_at, int(success), checked_at, status, int(success), error[:1000], next_run_at,
             checked_at, sub_id, worker_id),
        )
        c.commit()
    return get_subscription(sub_id)


def record_target_fetch_success(target_id: str, checked_at: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        c = _connect()
        c.execute(
            "UPDATE subscription_targets SET last_checked_at=?,failure_count=0,updated_at=? "
            "WHERE target_id=?",
            (checked_at, checked_at, target_id),
        )
        c.commit()
    return get_subscription_target(target_id)


def record_target_fetch_failure(
    target_id: str,
    checked_at: str,
    *,
    disable_at: int = 3,
) -> Optional[Dict[str, Any]]:
    current = get_subscription_target(target_id)
    if not current:
        return None
    next_count = int(current.get("failure_count") or 0) + 1
    should_disable = int(disable_at) >= 1 and next_count >= max(1, int(disable_at))
    with _LOCK:
        c = _connect()
        c.execute(
            "UPDATE subscription_targets SET last_checked_at=?,failure_count=?,"
            "enabled=CASE WHEN ?=1 THEN 0 ELSE enabled END,updated_at=? WHERE target_id=?",
            (checked_at, next_count, int(should_disable), checked_at, target_id),
        )
        remaining = c.execute(
            "SELECT COUNT(*) count FROM subscription_targets WHERE sub_id=? AND enabled=1",
            (current["sub_id"],),
        ).fetchone()["count"]
        if not remaining:
            c.execute(
                "UPDATE subscriptions SET enabled=0,last_status='needs_configuration',next_run_at='',"
                "updated_at=? WHERE sub_id=?",
                (checked_at, current["sub_id"]),
            )
        c.commit()
    return get_subscription_target(target_id)


def list_competitor_changes(
    sub_id: str,
    *,
    status: str = "",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM competitor_changes WHERE sub_id=?"
    args: List[Any] = [sub_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(limit, 500)))
    return [_change_dict(row) for row in _connect().execute(sql, args).fetchall()]


def update_competitor_change_status(change_id: str, status: str) -> Optional[Dict[str, Any]]:
    if status not in _CHANGE_STATUSES:
        raise ValueError(f"unsupported competitor change status: {status}")
    with _LOCK:
        c = _connect()
        c.execute("UPDATE competitor_changes SET status=? WHERE change_id=?", (status, change_id))
        c.commit()
    return get_competitor_change(change_id)


def monitoring_metrics() -> Dict[str, Any]:
    c = _connect()
    due = c.execute(
        "SELECT COUNT(*) count FROM subscriptions WHERE enabled=1 AND next_run_at!='' "
        "AND next_run_at<=?",
        (_utc_now(),),
    ).fetchone()["count"]
    run_totals = c.execute(
        "SELECT COUNT(*) runs,COALESCE(SUM(target_count),0) attempts,"
        "COALESCE(SUM(success_count),0) successes,"
        "COALESCE(SUM(not_modified_count),0) not_modified,"
        "COALESCE(SUM(minor_change_count),0) minor_changes,"
        "COALESCE(SUM(llm_failure_count),0) llm_failures,"
        "COALESCE(SUM(llm_tokens),0) llm_tokens,"
        "COALESCE(AVG(duration_ms),0) avg_duration_ms FROM monitor_runs"
    ).fetchone()
    run_status = {
        row["status"]: row["count"]
        for row in c.execute(
            "SELECT status,COUNT(*) count FROM monitor_runs GROUP BY status"
        ).fetchall()
    }
    changes = c.execute(
        "SELECT COUNT(*) total,"
        "COALESCE(SUM(CASE WHEN material=1 THEN 1 ELSE 0 END),0) material,"
        "COALESCE(SUM(CASE WHEN llm_status!='skipped' THEN 1 ELSE 0 END),0) llm_calls,"
        "COALESCE(SUM(CASE WHEN llm_status='failed' THEN 1 ELSE 0 END),0) llm_failed "
        "FROM competitor_changes"
    ).fetchone()
    snapshots = c.execute(
        "SELECT COUNT(*) count,COALESCE(SUM(content_length),0) content_bytes FROM page_snapshots"
    ).fetchone()
    attempts = int(run_totals["attempts"] or 0)
    successes = int(run_totals["successes"] or 0)
    llm_calls = int(changes["llm_calls"] or 0)
    return {
        "due_subscriptions": int(due),
        "runs_total": int(run_totals["runs"] or 0),
        "runs_by_status": run_status,
        "fetch_attempts": attempts,
        "fetch_successes": successes,
        "fetch_success_rate": round(successes / attempts, 4) if attempts else 0.0,
        "not_modified_count": int(run_totals["not_modified"] or 0),
        "not_modified_rate": round(int(run_totals["not_modified"] or 0) / successes, 4)
        if successes else 0.0,
        "changes_total": int(changes["total"] or 0),
        "material_changes": int(changes["material"] or 0),
        "minor_changes": int(run_totals["minor_changes"] or 0),
        "llm_classification_calls": llm_calls,
        "llm_classification_failures": int(changes["llm_failed"] or 0),
        "llm_failure_rate": round(int(changes["llm_failed"] or 0) / llm_calls, 4)
        if llm_calls else 0.0,
        "llm_tokens": int(run_totals["llm_tokens"] or 0),
        "average_run_duration_ms": round(float(run_totals["avg_duration_ms"] or 0), 1),
        "snapshot_count": int(snapshots["count"] or 0),
        "snapshot_content_bytes": int(snapshots["content_bytes"] or 0),
        "failing_targets": int(c.execute(
            "SELECT COUNT(*) count FROM subscription_targets WHERE failure_count>0"
        ).fetchone()["count"]),
    }


# ── 专家工作量看板 ──────────────────────────────────────
def bump_expert_stats(
    expert_ids: List[str],
    claims_by_author: Optional[Dict[str, int]] = None,
    evidence_by_collector: Optional[Dict[str, int]] = None,
) -> None:
    claims_by_author = claims_by_author or {}
    evidence_by_collector = evidence_by_collector or {}
    ids = set(expert_ids) | set(claims_by_author) | set(evidence_by_collector)
    with _LOCK:
        c = _connect()
        for eid in ids:
            c.execute(
                "INSERT INTO expert_stats(expert_id,missions,claims_authored,evidence_collected,last_active)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(expert_id) DO UPDATE SET"
                " missions=missions+excluded.missions,"
                " claims_authored=claims_authored+excluded.claims_authored,"
                " evidence_collected=evidence_collected+excluded.evidence_collected,"
                " last_active=excluded.last_active",
                (
                    eid,
                    1 if eid in expert_ids else 0,
                    claims_by_author.get(eid, 0),
                    evidence_by_collector.get(eid, 0),
                    _now(),
                ),
            )
        c.commit()


def expert_workload() -> List[Dict[str, Any]]:
    c = _connect()
    rows = c.execute(
        "SELECT * FROM expert_stats ORDER BY missions DESC, claims_authored DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ── Trace（可观测性）──────────────────────────────────────
def save_traces(task_id: str, report_id: str, spans: List[Dict[str, Any]]) -> None:
    if not spans:
        return
    with _LOCK:
        c = _connect()
        for s in spans:
            c.execute(
                "INSERT OR REPLACE INTO traces(span_id,task_id,report_id,seq,agent_id,stage,"
                "purpose,model,prompt,response,prompt_tokens,completion_tokens,total_tokens,"
                "latency_ms,decision,evidence_ids,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    s.get("span_id"), task_id, report_id, s.get("seq", 0),
                    s.get("agent_id", ""), s.get("stage", ""), s.get("purpose", ""),
                    s.get("model", ""), (s.get("prompt", "") or "")[:2000],
                    (s.get("response", "") or "")[:2000],
                    s.get("prompt_tokens", 0), s.get("completion_tokens", 0),
                    s.get("total_tokens", 0), s.get("latency_ms", 0),
                    s.get("decision", ""), json.dumps(s.get("evidence_ids", []), ensure_ascii=False),
                    s.get("ts", _now()),
                ),
            )
        c.commit()


def get_traces_by_task(task_id: str) -> List[Dict[str, Any]]:
    c = _connect()
    rows = c.execute("SELECT * FROM traces WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence_ids"] = json.loads(d.get("evidence_ids") or "[]")
        out.append(d)
    return out


def get_traces_by_report(report_id: str) -> List[Dict[str, Any]]:
    c = _connect()
    rows = c.execute("SELECT * FROM traces WHERE report_id=? ORDER BY seq", (report_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence_ids"] = json.loads(d.get("evidence_ids") or "[]")
        out.append(d)
    return out


# ── 报告反馈（人工修正率）────────────────────────────────
def save_report_feedback(report_id: str, edited_blocks: int, total_blocks: int,
                         data: Dict[str, Any]) -> None:
    with _LOCK:
        c = _connect()
        c.execute(
            "INSERT OR REPLACE INTO report_feedback(report_id,edited_blocks,total_blocks,data,updated_at)"
            " VALUES(?,?,?,?,?)",
            (report_id, edited_blocks, total_blocks,
             json.dumps(data, ensure_ascii=False), _now()),
        )
        c.commit()


def get_report_feedback(report_id: str) -> Optional[Dict[str, Any]]:
    c = _connect()
    row = c.execute("SELECT * FROM report_feedback WHERE report_id=?", (report_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["data"] = json.loads(d.get("data") or "{}")
    return d
