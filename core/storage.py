"""Transactional SQLite storage for Mai_life."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

SCHEMA_VERSION = 4


class LifeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "mai_life.db"
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    # 初始化可重复调用；同一 Runner 内不会重复打开 SQLite 连接。
    async def initialize(self) -> None:
        async with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if self._conn is None:
                self._open_checked()
            self._create_schema()

    # quick_check 失败时先保留损坏文件，再创建全新数据库。
    def _open_checked(self) -> None:
        try:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            result = conn.execute("PRAGMA quick_check").fetchone()
            if result and str(result[0]).lower() != "ok":
                raise sqlite3.DatabaseError(str(result[0]))
            self._conn = conn
        except sqlite3.DatabaseError:
            try:
                if self._conn:
                    self._conn.close()
            except Exception:
                pass
            if self.path.exists():
                backup = self.path.with_suffix(f".corrupt.{int(time.time())}.db")
                shutil.move(str(self.path), str(backup))
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("LifeStore is not initialized")
        return self._conn

    # 所有复合写入使用 IMMEDIATE 事务，防止状态只更新一半。
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # Schema 使用显式版本号，为后续迁移保留稳定入口。
    def _create_schema(self) -> None:
        existing = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if existing:
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            version = int(row[0]) if row else 0
            if version > SCHEMA_VERSION:
                self.conn.close()
                backup = self.path.with_suffix(f".future-v{version}.{int(time.time())}.db")
                shutil.move(str(self.path), str(backup))
                self._conn = sqlite3.connect(self.path, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS global_state(
              id INTEGER PRIMARY KEY CHECK(id=1), energy REAL NOT NULL, hunger REAL NOT NULL,
              mood_valence REAL NOT NULL, mood_arousal REAL NOT NULL,
              health_status TEXT NOT NULL, health_note TEXT NOT NULL,
              sleep_phase TEXT NOT NULL, current_location TEXT NOT NULL,
              current_activity TEXT NOT NULL, body_cycle TEXT NOT NULL,
              last_updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sleep_runtime(
              id INTEGER PRIMARY KEY CHECK(id=1), phase TEXT NOT NULL,
              started_at REAL NOT NULL, awake_grace_until REAL NOT NULL DEFAULT 0,
              woken_count INTEGER NOT NULL DEFAULT 0, last_event TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS dreams(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
              content TEXT NOT NULL, mood_delta REAL NOT NULL DEFAULT 0,
              energy_delta REAL NOT NULL DEFAULT 0, sleep_started_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS dream_fragments(
              id INTEGER PRIMARY KEY AUTOINCREMENT, dream_id INTEGER NOT NULL,
              position INTEGER NOT NULL, content TEXT NOT NULL,
              UNIQUE(dream_id,position)
            );
            CREATE TABLE IF NOT EXISTS daily_framework(
              id TEXT PRIMARY KEY, day TEXT NOT NULL, start_minute INTEGER NOT NULL,
              end_minute INTEGER NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL,
              location TEXT NOT NULL, energy_load REAL NOT NULL, shareability REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_framework_day ON daily_framework(day,start_minute);
            CREATE TABLE IF NOT EXISTS detailed_scenes(
              framework_id TEXT PRIMARY KEY, scene TEXT NOT NULL,
              state_deltas TEXT NOT NULL, created_at REAL NOT NULL,
              applied INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(framework_id) REFERENCES daily_framework(id)
            );
            CREATE TABLE IF NOT EXISTS proactive_opportunities(
              id TEXT PRIMARY KEY, framework_id TEXT NOT NULL, topic TEXT NOT NULL,
              motive TEXT NOT NULL, weight REAL NOT NULL, privacy TEXT NOT NULL,
              target_user_id TEXT NOT NULL DEFAULT '',
              expires_at REAL NOT NULL, consumed_by TEXT NOT NULL DEFAULT '',
              consumed_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_opportunity_active ON proactive_opportunities(expires_at,consumed_at);
            CREATE TABLE IF NOT EXISTS users(
              user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, proactive_enabled INTEGER NOT NULL,
              display_name TEXT NOT NULL, temperature REAL NOT NULL,
              role TEXT NOT NULL DEFAULT 'friend', daily_proactive_max INTEGER NOT NULL DEFAULT 1,
              quiet_start TEXT NOT NULL, quiet_end TEXT NOT NULL,
              stream_id TEXT NOT NULL DEFAULT '', last_user_message_at REAL NOT NULL DEFAULT 0,
              last_proactive_at REAL NOT NULL DEFAULT 0,
              proactive_day TEXT NOT NULL DEFAULT '', proactive_count INTEGER NOT NULL DEFAULT 0,
              last_relation_day TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS interaction_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
              created_at REAL NOT NULL, kind TEXT NOT NULL, hour INTEGER NOT NULL,
              content_summary TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_interaction_user_time ON interaction_events(user_id,created_at);
            CREATE TABLE IF NOT EXISTS proactive_events(
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, opportunity_id TEXT NOT NULL,
              stream_id TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
              expires_at REAL NOT NULL, sent_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_proactive_pending ON proactive_events(stream_id,status,expires_at);
            CREATE TABLE IF NOT EXISTS rest_backlogs(
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
              created_at REAL NOT NULL, summary TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS weather_cache(
              id INTEGER PRIMARY KEY CHECK(id=1), fetched_at REAL NOT NULL,
              location_name TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
              temperature REAL, weather_code INTEGER, description TEXT NOT NULL,
              raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wake_candidates(
              session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, message_id TEXT NOT NULL,
              reason TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_continuity(
              user_id TEXT PRIMARY KEY, intent TEXT NOT NULL DEFAULT '',
              unresolved_topics TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS image_summaries(
              image_hash TEXT PRIMARY KEY, summary TEXT NOT NULL, source_type TEXT NOT NULL,
              ownership_hint TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
              created_at REAL NOT NULL, expires_at REAL NOT NULL, current_until REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_image_session_current ON image_summaries(session_id,current_until);
            CREATE TABLE IF NOT EXISTS llm_usage_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
              source TEXT NOT NULL, task_name TEXT NOT NULL, model_name TEXT NOT NULL,
              request_type TEXT NOT NULL, prompt_tokens INTEGER NOT NULL DEFAULT 0,
              completion_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0,
              latency_ms REAL NOT NULL DEFAULT 0, success INTEGER NOT NULL, error_summary TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_llm_usage_time ON llm_usage_events(created_at,source,task_name);
            CREATE TABLE IF NOT EXISTS reply_turns(
              session_id TEXT NOT NULL, anchor_message_id TEXT NOT NULL, status TEXT NOT NULL,
              created_at REAL NOT NULL, expires_at REAL NOT NULL,
              PRIMARY KEY(session_id,anchor_message_id)
            );
            CREATE TABLE IF NOT EXISTS diary_entries(
              day TEXT PRIMARY KEY, created_at REAL NOT NULL, title TEXT NOT NULL,
              content TEXT NOT NULL, mood_summary TEXT NOT NULL,
              privacy TEXT NOT NULL DEFAULT 'private', source_digest TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS important_dates(
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
              event_name TEXT NOT NULL, event_date TEXT NOT NULL,
              recurrence TEXT NOT NULL DEFAULT 'none', source TEXT NOT NULL DEFAULT 'conversation',
              created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_important_date_unique
              ON important_dates(user_id,event_name,event_date,recurrence);
            CREATE INDEX IF NOT EXISTS idx_important_date_user ON important_dates(user_id,event_date);
            CREATE TABLE IF NOT EXISTS date_candidates(
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
              event_name TEXT NOT NULL, date_text TEXT NOT NULL,
              suggested_date TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 0,
              source_summary TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE INDEX IF NOT EXISTS idx_date_candidate_user ON date_candidates(user_id,status,created_at);
            CREATE TABLE IF NOT EXISTS skills(
              skill_name TEXT PRIMARY KEY, category TEXT NOT NULL,
              level REAL NOT NULL DEFAULT 0, evidence_count INTEGER NOT NULL DEFAULT 0,
              last_practiced_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS skill_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
              skill_name TEXT NOT NULL, source_kind TEXT NOT NULL,
              evidence_summary TEXT NOT NULL, evidence_key TEXT NOT NULL UNIQUE,
              gain REAL NOT NULL, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_skill_event_day ON skill_events(day,skill_name);
            CREATE TABLE IF NOT EXISTS date_trigger_events(
              date_id INTEGER NOT NULL, occurrence_date TEXT NOT NULL,
              lead_days INTEGER NOT NULL, created_at REAL NOT NULL,
              PRIMARY KEY(date_id,occurrence_date,lead_days)
            );
            CREATE TABLE IF NOT EXISTS memory_runtime(
              id INTEGER PRIMARY KEY CHECK(id=1), last_diary_day TEXT NOT NULL DEFAULT '',
              last_skill_day TEXT NOT NULL DEFAULT '', last_cleanup_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS news_items(
              id TEXT PRIMARY KEY, source_id TEXT NOT NULL, title TEXT NOT NULL,
              url TEXT NOT NULL, summary TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
              published_at REAL NOT NULL DEFAULT 0, fetched_at REAL NOT NULL,
              content_hash TEXT NOT NULL, relevance_score REAL NOT NULL DEFAULT 0,
              relevance_reason TEXT NOT NULL DEFAULT '', associated_at REAL NOT NULL DEFAULT 0,
              opportunity_id TEXT NOT NULL DEFAULT '', expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_news_pending ON news_items(associated_at,fetched_at);
            CREATE TABLE IF NOT EXISTS exploration_notes(
              id TEXT PRIMARY KEY, topic TEXT NOT NULL, query TEXT NOT NULL,
              summary TEXT NOT NULL, source_urls TEXT NOT NULL DEFAULT '[]',
              created_at REAL NOT NULL, relevance_score REAL NOT NULL DEFAULT 0,
              relevance_reason TEXT NOT NULL DEFAULT '', opportunity_id TEXT NOT NULL DEFAULT '',
              expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_exploration_time ON exploration_notes(created_at);
            CREATE TABLE IF NOT EXISTS information_source_runtime(
              source_key TEXT PRIMARY KEY, last_attempt_at REAL NOT NULL DEFAULT 0,
              last_success_at REAL NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0,
              next_retry_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
              etag TEXT NOT NULL DEFAULT '', last_modified TEXT NOT NULL DEFAULT ''
            );
            """
        )
        # v1 数据库可能已经存在 users 表；只补列，不重建用户数据。
        self._ensure_column("users", "role", "TEXT NOT NULL DEFAULT 'friend'")
        self._ensure_column("users", "daily_proactive_max", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("proactive_opportunities", "target_user_id", "TEXT NOT NULL DEFAULT ''")
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),),
        )
        now = time.time()
        self.conn.execute(
            """INSERT OR IGNORE INTO global_state VALUES
            (1,70,20,0,0.65,'normal','状态正常','awake','家里','自由活动','未启用',?)""",
            (now,),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO sleep_runtime VALUES(1,'awake',?,0,0,'初始化')",
            (now,),
        )
        self.conn.execute("INSERT OR IGNORE INTO memory_runtime(id) VALUES(1)")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        """幂等补充 SQLite 列，避免测试阶段升级时覆盖已有生活数据。"""
        columns = {str(row[1]) for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    async def close(self) -> None:
        async with self._lock:
            if self._conn:
                self._conn.commit()
                self._conn.close()
                self._conn = None

    async def get_state(self) -> dict[str, Any]:
        async with self._lock:
            row = self.conn.execute("SELECT * FROM global_state WHERE id=1").fetchone()
            return dict(row) if row else {}

    async def save_state(self, state: dict[str, Any]) -> None:
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    """UPDATE global_state SET energy=?,hunger=?,mood_valence=?,mood_arousal=?,
                    health_status=?,health_note=?,sleep_phase=?,current_location=?,current_activity=?,
                    body_cycle=?,last_updated_at=? WHERE id=1""",
                    tuple(state[k] for k in (
                        "energy","hunger","mood_valence","mood_arousal","health_status",
                        "health_note","sleep_phase","current_location","current_activity",
                        "body_cycle","last_updated_at"
                    )),
                )

    async def get_sleep_runtime(self) -> dict[str, Any]:
        async with self._lock:
            row = self.conn.execute("SELECT * FROM sleep_runtime WHERE id=1").fetchone()
            return dict(row) if row else {}

    async def save_sleep_runtime(self, runtime: dict[str, Any]) -> None:
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    """UPDATE sleep_runtime SET phase=?,started_at=?,awake_grace_until=?,
                    woken_count=?,last_event=? WHERE id=1""",
                    (runtime["phase"],runtime["started_at"],runtime.get("awake_grace_until",0),
                     runtime.get("woken_count",0),runtime.get("last_event","")),
                )

    async def add_dream(self, content: str, mood_delta: float, energy_delta: float,
                        sleep_started_at: float, fragments: list[str] | None = None) -> int:
        async with self._lock:
            with self._tx() as conn:
                cursor=conn.execute(
                    "INSERT INTO dreams(created_at,content,mood_delta,energy_delta,sleep_started_at) VALUES(?,?,?,?,?)",
                    (time.time(), content[:1000], mood_delta, energy_delta, sleep_started_at),
                )
                dream_id=int(cursor.lastrowid)
                clean=[str(item).strip()[:300] for item in (fragments or []) if str(item).strip()][:5]
                conn.executemany(
                    "INSERT INTO dream_fragments(dream_id,position,content) VALUES(?,?,?)",
                    [(dream_id,index,item) for index,item in enumerate(clean)],
                )
                return dream_id

    async def latest_dream(self) -> dict[str, Any]:
        async with self._lock:
            row = self.conn.execute("SELECT * FROM dreams ORDER BY created_at DESC LIMIT 1").fetchone()
            if not row:return {}
            data=dict(row)
            fragments=self.conn.execute(
                "SELECT content FROM dream_fragments WHERE dream_id=? ORDER BY position",(data["id"],)
            ).fetchall()
            data["fragments"]=[str(item[0]) for item in fragments]
            return data

    async def dreams_between(self, start: float, end: float) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                "SELECT * FROM dreams WHERE created_at>=? AND created_at<? ORDER BY created_at",(start,end)
            ).fetchall()
            result=[]
            for row in rows:
                data=dict(row)
                fragments=self.conn.execute(
                    "SELECT content FROM dream_fragments WHERE dream_id=? ORDER BY position",(data["id"],)
                ).fetchall()
                data["fragments"]=[str(item[0]) for item in fragments]
                result.append(data)
            return result

    # 替换日程时同步清理旧场景和机会，避免旧节点继续触发。
    async def replace_framework(self, day: str, nodes: list[dict[str, Any]]) -> None:
        async with self._lock:
            with self._tx() as conn:
                old_ids = [r[0] for r in conn.execute("SELECT id FROM daily_framework WHERE day=?", (day,))]
                if old_ids:
                    marks = ",".join("?" for _ in old_ids)
                    conn.execute(f"DELETE FROM proactive_opportunities WHERE framework_id IN ({marks})", old_ids)
                    conn.execute(f"DELETE FROM detailed_scenes WHERE framework_id IN ({marks})", old_ids)
                conn.execute("DELETE FROM daily_framework WHERE day=?", (day,))
                conn.executemany(
                    """INSERT INTO daily_framework(id,day,start_minute,end_minute,kind,summary,location,energy_load,shareability)
                    VALUES(:id,:day,:start_minute,:end_minute,:kind,:summary,:location,:energy_load,:shareability)""",
                    nodes,
                )

    async def get_framework(self, day: str) -> list[dict[str, Any]]:
        async with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM daily_framework WHERE day=? ORDER BY start_minute", (day,)
            ).fetchall()
            return [dict(r) for r in rows]

    async def save_scene(self, framework_id: str, scene: str, deltas: dict[str, Any], opportunities: list[dict[str, Any]]) -> None:
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO detailed_scenes(framework_id,scene,state_deltas,created_at,applied) VALUES(?,?,?,?,0)",
                    (framework_id, scene, json.dumps(deltas, ensure_ascii=False), time.time()),
                )
                for item in opportunities:
                    conn.execute(
                        """INSERT OR IGNORE INTO proactive_opportunities
                        (id,framework_id,topic,motive,weight,privacy,target_user_id,expires_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (item["id"], framework_id, item["topic"], item["motive"], item["weight"],
                         item.get("privacy","normal"),item.get("target_user_id",""),item["expires_at"]),
                    )

    async def get_scene(self, framework_id: str) -> dict[str, Any]:
        async with self._lock:
            row = self.conn.execute("SELECT * FROM detailed_scenes WHERE framework_id=?", (framework_id,)).fetchone()
            if not row:
                return {}
            data = dict(row)
            data["state_deltas"] = json.loads(data.get("state_deltas") or "{}")
            return data

    async def completed_unapplied_scenes(self, day: str, minute: int) -> list[dict[str, Any]]:
        async with self._lock:
            rows = self.conn.execute(
                """SELECT s.*,f.end_minute FROM detailed_scenes s JOIN daily_framework f ON f.id=s.framework_id
                WHERE f.day=? AND f.end_minute<=? AND s.applied=0""", (day, minute)
            ).fetchall()
            result=[]
            for row in rows:
                item=dict(row); item["state_deltas"]=json.loads(item["state_deltas"] or "{}"); result.append(item)
            return result

    async def mark_scene_applied(self, framework_id: str) -> None:
        async with self._lock:
            self.conn.execute("UPDATE detailed_scenes SET applied=1 WHERE framework_id=?", (framework_id,))
            self.conn.commit()

    async def add_opportunity(self, item: dict[str, Any]) -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT OR IGNORE INTO proactive_opportunities
                (id,framework_id,topic,motive,weight,privacy,target_user_id,expires_at) VALUES(?,?,?,?,?,?,?,?)""",
                (item["id"],item["framework_id"],item["topic"],item["motive"],item["weight"],item.get("privacy","normal"),
                 item.get("target_user_id",""),item["expires_at"]),
            )
            self.conn.commit()

    async def active_opportunities(self, now: float) -> list[dict[str, Any]]:
        async with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM proactive_opportunities WHERE consumed_at=0 AND expires_at>? ORDER BY weight DESC",
                (now,),
            ).fetchall()
            return [dict(r) for r in rows]

    # 条件更新保证同一生活事件最多被一个用户消费。
    async def consume_opportunity(self, opportunity_id: str, user_id: str, now: float) -> bool:
        async with self._lock:
            cur = self.conn.execute(
                "UPDATE proactive_opportunities SET consumed_by=?,consumed_at=? WHERE id=? AND consumed_at=0",
                (user_id, now, opportunity_id),
            )
            self.conn.commit()
            return cur.rowcount == 1

    async def release_opportunity(self, opportunity_id: str) -> None:
        async with self._lock:
            self.conn.execute("UPDATE proactive_opportunities SET consumed_by='',consumed_at=0 WHERE id=?", (opportunity_id,))
            self.conn.commit()

    async def sync_users(self, profiles: Iterable[Any]) -> None:
        async with self._lock:
            with self._tx() as conn:
                seen=[]
                for p in profiles:
                    uid=str(p.user_id).strip()
                    if not uid: continue
                    seen.append(uid)
                    role=str(getattr(p,"role","friend") or "friend")
                    configured_limit=int(getattr(p,"daily_proactive_max",-1))
                    daily_limit=(2 if role=="owner" else 1) if configured_limit<0 else configured_limit
                    conn.execute(
                        """INSERT INTO users(user_id,enabled,proactive_enabled,display_name,temperature,role,daily_proactive_max,quiet_start,quiet_end)
                        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                        enabled=excluded.enabled,proactive_enabled=excluded.proactive_enabled,
                        display_name=excluded.display_name,role=excluded.role,daily_proactive_max=excluded.daily_proactive_max,
                        quiet_start=excluded.quiet_start,quiet_end=excluded.quiet_end""",
                        (uid,int(p.enabled),int(p.proactive_enabled),p.display_name,float(p.initial_temperature),role,daily_limit,p.quiet_start,p.quiet_end),
                    )
                if seen:
                    marks=",".join("?" for _ in seen)
                    conn.execute(f"UPDATE users SET enabled=0,proactive_enabled=0 WHERE user_id NOT IN ({marks})", seen)
                else:
                    conn.execute("UPDATE users SET enabled=0,proactive_enabled=0")

    async def get_user(self, user_id: str) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM users WHERE user_id=?",(user_id,)).fetchone()
            return dict(row) if row else {}

    async def list_users(self, proactive_only: bool=False) -> list[dict[str, Any]]:
        async with self._lock:
            sql="SELECT * FROM users WHERE enabled=1"
            if proactive_only: sql += " AND proactive_enabled=1"
            return [dict(r) for r in self.conn.execute(sql).fetchall()]

    async def set_user_stream(self, user_id: str, stream_id: str) -> None:
        async with self._lock:
            self.conn.execute("UPDATE users SET stream_id=? WHERE user_id=?",(stream_id,user_id)); self.conn.commit()

    # 同时记录活跃时段，并识别用户是否回应了近期主动消息。
    async def record_interaction(self, user_id: str, text: str, now: float, hour: int) -> None:
        async with self._lock:
            with self._tx() as conn:
                conn.execute("UPDATE users SET last_user_message_at=? WHERE user_id=?",(now,user_id))
                conn.execute(
                    "INSERT INTO interaction_events(user_id,created_at,kind,hour,content_summary) VALUES(?,?,?,?,?)",
                    (user_id,now,"message",hour,text[:240]),
                )
                pending=conn.execute(
                    "SELECT id,sent_at FROM proactive_events WHERE user_id=? AND status='sent' AND sent_at>? ORDER BY sent_at DESC LIMIT 1",
                    (user_id,now-7200),
                ).fetchone()
                if pending:
                    exists=conn.execute(
                        "SELECT 1 FROM interaction_events WHERE user_id=? AND kind='proactive_response' AND created_at>?",
                        (user_id,float(pending[1])),
                    ).fetchone()
                    if not exists:
                        conn.execute(
                            "INSERT INTO interaction_events(user_id,created_at,kind,hour,content_summary) VALUES(?,?,?,?,?)",
                            (user_id,now,"proactive_response",hour,"回应了主动消息"),
                        )

    async def active_hours(self, user_id: str, since: float) -> dict[int,int]:
        async with self._lock:
            rows=self.conn.execute(
                "SELECT hour,COUNT(*) c FROM interaction_events WHERE user_id=? AND created_at>? AND kind='message' GROUP BY hour",
                (user_id,since),
            ).fetchall()
            return {int(r[0]):int(r[1]) for r in rows}

    async def recent_interactions(self, user_id: str, limit: int=8) -> list[str]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT content_summary FROM interaction_events
                WHERE user_id=? AND kind='message' ORDER BY created_at DESC LIMIT ?""",
                (user_id,limit),
            ).fetchall()
            return [str(row[0]) for row in reversed(rows) if str(row[0]).strip()]

    async def add_rest_backlog(self, user_id: str, summary: str, now: float) -> None:
        async with self._lock:
            self.conn.execute("INSERT INTO rest_backlogs(user_id,created_at,summary) VALUES(?,?,?)",(user_id,now,summary[:240])); self.conn.commit()

    async def consume_rest_backlogs(self, user_id: str) -> list[str]:
        async with self._lock:
            with self._tx() as conn:
                rows=conn.execute("SELECT id,summary FROM rest_backlogs WHERE user_id=? AND consumed=0 ORDER BY created_at LIMIT 3",(user_id,)).fetchall()
                if rows:
                    conn.executemany("UPDATE rest_backlogs SET consumed=1 WHERE id=?",[(r[0],) for r in rows])
                return [str(r[1]) for r in rows]

    async def peek_rest_backlogs(self, user_id: str) -> list[str]:
        async with self._lock:
            rows=self.conn.execute("SELECT summary FROM rest_backlogs WHERE user_id=? AND consumed=0 ORDER BY created_at LIMIT 3",(user_id,)).fetchall()
            return [str(r[0]) for r in rows]

    async def add_proactive_pending(self, event_id: str, user_id: str, opportunity_id: str, stream_id: str, now: float, expires_at: float) -> None:
        async with self._lock:
            self.conn.execute(
                "INSERT INTO proactive_events(id,user_id,opportunity_id,stream_id,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (event_id,user_id,opportunity_id,stream_id,"pending",now,expires_at),
            ); self.conn.commit()

    # 只有 send_service.after_send 确认平台发送成功后，才增加实际主动额度。
    async def mark_pending_sent(self, stream_id: str, now: float, day: str = "") -> bool:
        async with self._lock:
            # 普通被动回复没有 pending 记录，只读查询不会创建 SQLite journal。
            row=self.conn.execute(
                "SELECT id,user_id FROM proactive_events WHERE stream_id=? AND status='pending' AND expires_at>? ORDER BY created_at DESC LIMIT 1",
                (stream_id,now),
            ).fetchone()
            if not row:
                return False
            with self._tx() as conn:
                conn.execute("UPDATE proactive_events SET status='sent',sent_at=? WHERE id=?",(now,row[0]))
                day=day or time.strftime("%Y-%m-%d",time.localtime(now))
                user=conn.execute("SELECT proactive_day FROM users WHERE user_id=?",(row[1],)).fetchone()
                if user and user[0]==day:
                    conn.execute("UPDATE users SET proactive_count=proactive_count+1,last_proactive_at=? WHERE user_id=?",(now,row[1]))
                else:
                    conn.execute("UPDATE users SET proactive_day=?,proactive_count=1,last_proactive_at=? WHERE user_id=?",(day,now,row[1]))
                return True

    async def expire_pending(self, now: float) -> None:
        async with self._lock:
            self.conn.execute("UPDATE proactive_events SET status='expired' WHERE status='pending' AND expires_at<=?",(now,)); self.conn.commit()

    async def set_wake_candidate(self, session_id: str, user_id: str, message_id: str, reason: str, now: float, expires_at: float) -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO wake_candidates
                (session_id,user_id,message_id,reason,created_at,expires_at) VALUES(?,?,?,?,?,?)""",
                (session_id,user_id,message_id,reason[:240],now,expires_at),
            )
            self.conn.commit()

    async def pop_wake_candidate(self, session_id: str, now: float) -> dict[str, Any]:
        """只有真实发送时消费待醒候选；普通发送只执行一次只读查询。"""
        async with self._lock:
            row=self.conn.execute(
                "SELECT * FROM wake_candidates WHERE session_id=? AND expires_at>?",
                (session_id,now),
            ).fetchone()
            if not row:
                self.conn.execute("DELETE FROM wake_candidates WHERE session_id=? AND expires_at<=?",(session_id,now))
                self.conn.commit()
                return {}
            with self._tx() as conn:
                conn.execute("DELETE FROM wake_candidates WHERE session_id=?",(session_id,))
            return dict(row)

    async def clear_wake_candidate(self, session_id: str) -> None:
        async with self._lock:
            self.conn.execute("DELETE FROM wake_candidates WHERE session_id=?",(session_id,))
            self.conn.commit()

    async def get_continuity(self, user_id: str) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM conversation_continuity WHERE user_id=?",(user_id,)).fetchone()
            if not row:return {"intent":"","unresolved_topics":[],"updated_at":0}
            data=dict(row)
            try:data["unresolved_topics"]=json.loads(data.get("unresolved_topics") or "[]")
            except (TypeError,json.JSONDecodeError):data["unresolved_topics"]=[]
            return data

    async def save_continuity(self, user_id: str, intent: str, topics: list[str], now: float) -> None:
        clean_topics=[str(item).strip()[:180] for item in topics if str(item).strip()][:5]
        async with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO conversation_continuity
                (user_id,intent,unresolved_topics,updated_at) VALUES(?,?,?,?)""",
                (user_id,intent[:80],json.dumps(clean_topics,ensure_ascii=False),now),
            )
            self.conn.commit()

    async def save_image_summary(self, image_hash: str, summary: str, source_type: str, ownership_hint: str,
                                 session_id: str, now: float, expires_at: float, current_until: float) -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO image_summaries
                (image_hash,summary,source_type,ownership_hint,session_id,created_at,expires_at,current_until)
                VALUES(?,?,?,?,?,?,?,?)""",
                (image_hash,summary[:1000],source_type[:40],ownership_hint[:240],session_id,now,expires_at,current_until),
            )
            self.conn.commit()

    async def get_image_summary(self, image_hash: str, now: float) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute(
                "SELECT * FROM image_summaries WHERE image_hash=? AND expires_at>?",(image_hash,now)
            ).fetchone()
            return dict(row) if row else {}

    async def current_image_summaries(self, session_id: str, now: float, limit: int=3) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT * FROM image_summaries WHERE session_id=? AND expires_at>? AND current_until>?
                ORDER BY created_at DESC LIMIT ?""",(session_id,now,now,limit)
            ).fetchall()
            return [dict(row) for row in rows]

    async def cleanup_runtime_records(self, now: float, usage_before: float) -> None:
        async with self._lock:
            with self._tx() as conn:
                conn.execute("DELETE FROM image_summaries WHERE expires_at<=?",(now,))
                conn.execute("DELETE FROM wake_candidates WHERE expires_at<=?",(now,))
                conn.execute("DELETE FROM reply_turns WHERE expires_at<=?",(now,))
                conn.execute("DELETE FROM llm_usage_events WHERE created_at<?",(usage_before,))

    async def record_llm_usage(self, *, created_at: float, source: str, task_name: str, model_name: str,
                               request_type: str, prompt_tokens: int, completion_tokens: int,
                               total_tokens: int, latency_ms: float, success: bool, error_summary: str="") -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT INTO llm_usage_events
                (created_at,source,task_name,model_name,request_type,prompt_tokens,completion_tokens,total_tokens,latency_ms,success,error_summary)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (created_at,source[:32],task_name[:80],model_name[:160],request_type[:80],max(0,int(prompt_tokens)),
                 max(0,int(completion_tokens)),max(0,int(total_tokens)),max(0,float(latency_ms)),int(success),error_summary[:300]),
            )
            self.conn.commit()

    async def usage_summary(self, since: float) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT source,task_name,COUNT(*) calls,SUM(success) successes,
                SUM(prompt_tokens) prompt_tokens,SUM(completion_tokens) completion_tokens,
                SUM(total_tokens) total_tokens,AVG(latency_ms) avg_latency_ms
                FROM llm_usage_events WHERE created_at>=? GROUP BY source,task_name ORDER BY total_tokens DESC""",
                (since,),
            ).fetchall()
            return [dict(row) for row in rows]

    async def reserve_reply_turn(self, session_id: str, anchor_message_id: str, now: float, expires_at: float) -> bool:
        if not session_id or not anchor_message_id:return True
        async with self._lock:
            with self._tx() as conn:
                conn.execute("DELETE FROM reply_turns WHERE expires_at<=?",(now,))
                row=conn.execute(
                    "SELECT status FROM reply_turns WHERE session_id=? AND anchor_message_id=?",
                    (session_id,anchor_message_id),
                ).fetchone()
                if row:return False
                conn.execute(
                    "INSERT INTO reply_turns(session_id,anchor_message_id,status,created_at,expires_at) VALUES(?,?,?,?,?)",
                    (session_id,anchor_message_id,"generated",now,expires_at),
                )
                return True

    async def release_reply_turn(self, session_id: str, anchor_message_id: str) -> None:
        if not session_id or not anchor_message_id:return
        async with self._lock:
            self.conn.execute(
                "DELETE FROM reply_turns WHERE session_id=? AND anchor_message_id=?",
                (session_id,anchor_message_id),
            )
            self.conn.commit()

    async def save_diary(self, day: str, title: str, content: str, mood_summary: str,
                         source_digest: str, created_at: float) -> None:
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO diary_entries
                    (day,created_at,title,content,mood_summary,privacy,source_digest)
                    VALUES(?,?,?,?,?,'private',?)""",
                    (day,created_at,title[:120],content[:4000],mood_summary[:300],source_digest[:128]),
                )
                conn.execute("UPDATE memory_runtime SET last_diary_day=? WHERE id=1",(day,))

    async def get_diary(self, day: str) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM diary_entries WHERE day=?",(day,)).fetchone()
            return dict(row) if row else {}

    async def list_diaries(self, limit: int=7) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                "SELECT * FROM diary_entries ORDER BY day DESC LIMIT ?",(max(1,min(100,int(limit))),)
            ).fetchall()
            return [dict(row) for row in rows]

    async def scenes_for_day(self, day: str) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT f.kind,f.summary,f.location,s.scene,s.applied
                FROM daily_framework f LEFT JOIN detailed_scenes s ON s.framework_id=f.id
                WHERE f.day=? ORDER BY f.start_minute""",(day,)
            ).fetchall()
            return [dict(row) for row in rows]

    async def interaction_counts(self, start: float, end: float) -> list[dict[str, Any]]:
        """日记只读取交互数量，不读取用户消息摘要或原文。"""
        async with self._lock:
            rows=self.conn.execute(
                """SELECT user_id,kind,COUNT(*) count FROM interaction_events
                WHERE created_at>=? AND created_at<? GROUP BY user_id,kind""",(start,end)
            ).fetchall()
            return [dict(row) for row in rows]

    async def add_important_date(self, user_id: str, event_name: str, event_date: str,
                                 recurrence: str, source: str, now: float) -> int:
        clean_recurrence=recurrence if recurrence in {"none","annual"} else "none"
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO important_dates
                    (user_id,event_name,event_date,recurrence,source,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (user_id,event_name[:120],event_date,clean_recurrence,source[:40],now,now),
                )
                row=conn.execute(
                    """SELECT id FROM important_dates
                    WHERE user_id=? AND event_name=? AND event_date=? AND recurrence=?""",
                    (user_id,event_name[:120],event_date,clean_recurrence),
                ).fetchone()
                return int(row[0]) if row else 0

    async def list_important_dates(self, user_id: str="") -> list[dict[str, Any]]:
        async with self._lock:
            if user_id:
                rows=self.conn.execute(
                    "SELECT * FROM important_dates WHERE user_id=? ORDER BY event_date,event_name",(user_id,)
                ).fetchall()
            else:
                rows=self.conn.execute("SELECT * FROM important_dates ORDER BY event_date,event_name").fetchall()
            return [dict(row) for row in rows]

    async def remove_important_date(self, date_id: int, user_id: str="") -> bool:
        async with self._lock:
            with self._tx() as conn:
                if user_id:
                    cursor=conn.execute("DELETE FROM important_dates WHERE id=? AND user_id=?",(date_id,user_id))
                else:
                    cursor=conn.execute("DELETE FROM important_dates WHERE id=?",(date_id,))
                if cursor.rowcount==1:
                    conn.execute("DELETE FROM date_trigger_events WHERE date_id=?",(date_id,))
                    conn.execute(
                        "DELETE FROM proactive_opportunities WHERE framework_id=? AND consumed_at=0",
                        (f"important-date:{date_id}",),
                    )
                return cursor.rowcount==1

    async def add_date_candidate(self, user_id: str, event_name: str, date_text: str,
                                 suggested_date: str, confidence: float, source_summary: str,
                                 now: float) -> int:
        async with self._lock:
            existing=self.conn.execute(
                """SELECT id FROM date_candidates WHERE user_id=? AND event_name=? AND date_text=?
                AND status='pending' ORDER BY created_at DESC LIMIT 1""",
                (user_id,event_name[:120],date_text[:120]),
            ).fetchone()
            if existing:return int(existing[0])
            cursor=self.conn.execute(
                """INSERT INTO date_candidates
                (user_id,event_name,date_text,suggested_date,confidence,source_summary,created_at,status)
                VALUES(?,?,?,?,?,?,?,'pending')""",
                (user_id,event_name[:120],date_text[:120],suggested_date[:10],max(0,min(1,float(confidence))),
                 source_summary[:300],now),
            )
            self.conn.commit(); return int(cursor.lastrowid)

    async def list_date_candidates(self, user_id: str, status: str="pending") -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT * FROM date_candidates WHERE user_id=? AND status=?
                ORDER BY created_at DESC""",(user_id,status)
            ).fetchall()
            return [dict(row) for row in rows]

    async def confirm_date_candidate(self, candidate_id: int, user_id: str,
                                     event_date: str, now: float) -> int:
        """候选确认与正式日期写入同一事务，避免出现半确认状态。"""
        async with self._lock:
            with self._tx() as conn:
                row=conn.execute(
                    "SELECT * FROM date_candidates WHERE id=? AND user_id=? AND status='pending'",
                    (candidate_id,user_id),
                ).fetchone()
                if not row:return 0
                recurrence="annual" if any(word in str(row["event_name"]) for word in ("生日","纪念日")) else "none"
                conn.execute(
                    """INSERT OR IGNORE INTO important_dates
                    (user_id,event_name,event_date,recurrence,source,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (user_id,row["event_name"],event_date,recurrence,"confirmed_candidate",now,now),
                )
                saved=conn.execute(
                    """SELECT id FROM important_dates
                    WHERE user_id=? AND event_name=? AND event_date=? AND recurrence=?""",
                    (user_id,row["event_name"],event_date,recurrence),
                ).fetchone()
                conn.execute("UPDATE date_candidates SET status='confirmed' WHERE id=?",(candidate_id,))
                return int(saved[0]) if saved else 0

    async def add_skill_evidence(self, day: str, skill_name: str, category: str,
                                 source_kind: str, evidence_summary: str, evidence_key: str,
                                 gain: float, daily_max: float, now: float) -> bool:
        async with self._lock:
            with self._tx() as conn:
                if conn.execute("SELECT 1 FROM skill_events WHERE evidence_key=?",(evidence_key,)).fetchone():
                    return False
                gained=float(conn.execute(
                    "SELECT COALESCE(SUM(gain),0) FROM skill_events WHERE day=? AND skill_name=?",
                    (day,skill_name),
                ).fetchone()[0])
                applied=max(0,min(float(gain),max(0,float(daily_max)-gained)))
                if applied<=0:return False
                conn.execute(
                    """INSERT INTO skill_events
                    (day,skill_name,source_kind,evidence_summary,evidence_key,gain,created_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (day,skill_name[:120],source_kind[:40],evidence_summary[:300],evidence_key[:80],applied,now),
                )
                conn.execute(
                    """INSERT INTO skills(skill_name,category,level,evidence_count,last_practiced_at,updated_at)
                    VALUES(?,?,?,1,?,?)
                    ON CONFLICT(skill_name) DO UPDATE SET
                    category=excluded.category,level=MIN(100,skills.level+excluded.level),
                    evidence_count=skills.evidence_count+1,last_practiced_at=excluded.last_practiced_at,
                    updated_at=excluded.updated_at""",
                    (skill_name[:120],category[:80],applied,now,now),
                )
                return True

    async def list_skills(self, limit: int=30) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                "SELECT * FROM skills ORDER BY level DESC,last_practiced_at DESC LIMIT ?",
                (max(1,min(100,int(limit))),),
            ).fetchall()
            return [dict(row) for row in rows]

    async def memory_runtime(self) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM memory_runtime WHERE id=1").fetchone()
            return dict(row) if row else {}

    async def mark_skill_day(self, day: str) -> None:
        async with self._lock:
            self.conn.execute("UPDATE memory_runtime SET last_skill_day=? WHERE id=1",(day,)); self.conn.commit()

    async def reserve_date_trigger(self, date_id: int, occurrence_date: str,
                                   lead_days: int, now: float) -> bool:
        async with self._lock:
            cursor=self.conn.execute(
                """INSERT OR IGNORE INTO date_trigger_events
                (date_id,occurrence_date,lead_days,created_at) VALUES(?,?,?,?)""",
                (date_id,occurrence_date,lead_days,now),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def add_date_opportunity_once(self, date_id: int, occurrence_date: str, lead_days: int,
                                        item: dict[str, Any], now: float) -> bool:
        """日期触发标记和专属主动契机必须一起成功或一起回滚。"""
        async with self._lock:
            with self._tx() as conn:
                cursor=conn.execute(
                    """INSERT OR IGNORE INTO date_trigger_events
                    (date_id,occurrence_date,lead_days,created_at) VALUES(?,?,?,?)""",
                    (date_id,occurrence_date,lead_days,now),
                )
                if cursor.rowcount!=1:return False
                conn.execute(
                    """INSERT OR IGNORE INTO proactive_opportunities
                    (id,framework_id,topic,motive,weight,privacy,target_user_id,expires_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (item["id"],item["framework_id"],item["topic"],item["motive"],item["weight"],
                     item.get("privacy","target_only"),item.get("target_user_id",""),item["expires_at"]),
                )
                return True

    async def cleanup_date_candidates(self, before: float) -> None:
        async with self._lock:
            self.conn.execute(
                "DELETE FROM date_candidates WHERE status='pending' AND created_at<?",(before,)
            )
            self.conn.execute("UPDATE memory_runtime SET last_cleanup_at=? WHERE id=1",(time.time(),))
            self.conn.commit()

    async def upsert_news_item(self, item: dict[str, Any]) -> bool:
        async with self._lock:
            existing=self.conn.execute("SELECT content_hash FROM news_items WHERE id=?",(item["id"],)).fetchone()
            self.conn.execute(
                """INSERT INTO news_items
                (id,source_id,title,url,summary,content,published_at,fetched_at,content_hash,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                source_id=excluded.source_id,title=excluded.title,url=excluded.url,
                summary=excluded.summary,content=excluded.content,published_at=excluded.published_at,
                fetched_at=excluded.fetched_at,content_hash=excluded.content_hash,expires_at=excluded.expires_at""",
                (item["id"],item["source_id"][:120],item["title"][:500],item["url"][:2000],
                 item.get("summary","")[:3000],item.get("content","")[:16000],float(item.get("published_at") or 0),
                 float(item["fetched_at"]),item["content_hash"][:80],float(item["expires_at"])),
            )
            self.conn.commit(); return existing is None or str(existing[0])!=str(item["content_hash"])

    async def pending_news_items(self, now: float, limit: int=20) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT * FROM news_items WHERE associated_at=0 AND expires_at>?
                ORDER BY published_at DESC,fetched_at DESC LIMIT ?""",(now,max(1,min(100,int(limit))))
            ).fetchall()
            return [dict(row) for row in rows]

    async def mark_news_associated(self, item_id: str, score: float, reason: str,
                                   opportunity_id: str, now: float) -> None:
        async with self._lock:
            self.conn.execute(
                """UPDATE news_items SET relevance_score=?,relevance_reason=?,associated_at=?,opportunity_id=?
                WHERE id=?""",(max(0,min(1,float(score))),reason[:1000],now,opportunity_id[:80],item_id)
            )
            self.conn.commit()

    async def update_news_summary(self, item_id: str, summary: str) -> None:
        async with self._lock:
            self.conn.execute("UPDATE news_items SET summary=? WHERE id=?",(summary[:3000],item_id))
            self.conn.commit()

    async def recent_news_items(self, now: float, limit: int=10, *, associated_only: bool=False) -> list[dict[str, Any]]:
        async with self._lock:
            clause=" AND associated_at>0" if associated_only else ""
            rows=self.conn.execute(
                f"""SELECT * FROM news_items WHERE expires_at>?{clause}
                ORDER BY published_at DESC,fetched_at DESC LIMIT ?""",(now,max(1,min(100,int(limit))))
            ).fetchall()
            return [dict(row) for row in rows]

    async def save_exploration_note(self, item: dict[str, Any]) -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO exploration_notes
                (id,topic,query,summary,source_urls,created_at,relevance_score,relevance_reason,opportunity_id,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item["id"],item["topic"][:240],item["query"][:240],item["summary"][:5000],
                 json.dumps(item.get("source_urls") or [],ensure_ascii=False),float(item["created_at"]),
                 max(0,min(1,float(item.get("relevance_score") or 0))),item.get("relevance_reason","")[:1000],
                 item.get("opportunity_id","")[:80],float(item["expires_at"])),
            )
            self.conn.commit()

    async def exploration_count(self, start: float, end: float) -> int:
        async with self._lock:
            row=self.conn.execute(
                "SELECT COUNT(*) FROM exploration_notes WHERE created_at>=? AND created_at<?",(start,end)
            ).fetchone()
            return int(row[0]) if row else 0

    async def recent_exploration_notes(self, now: float, limit: int=10) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT * FROM exploration_notes WHERE expires_at>?
                ORDER BY created_at DESC LIMIT ?""",(now,max(1,min(100,int(limit))))
            ).fetchall()
            result=[]
            for row in rows:
                item=dict(row)
                try:item["source_urls"]=json.loads(item.get("source_urls") or "[]")
                except (TypeError,json.JSONDecodeError):item["source_urls"]=[]
                result.append(item)
            return result

    async def get_information_source_runtime(self, source_key: str) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute(
                "SELECT * FROM information_source_runtime WHERE source_key=?",(source_key,)
            ).fetchone()
            return dict(row) if row else {"source_key":source_key,"last_attempt_at":0,"last_success_at":0,
                                          "failure_count":0,"next_retry_at":0,"last_error":"","etag":"","last_modified":""}

    async def save_information_source_runtime(self, source_key: str, *, now: float, success: bool,
                                              next_retry_at: float, error: str="", etag: str="",
                                              last_modified: str="") -> None:
        async with self._lock:
            previous=self.conn.execute(
                "SELECT * FROM information_source_runtime WHERE source_key=?",(source_key,)
            ).fetchone()
            failures=0 if success else int(previous["failure_count"] if previous else 0)+1
            last_success=now if success else float(previous["last_success_at"] if previous else 0)
            old_etag=str(previous["etag"] if previous else ""); old_modified=str(previous["last_modified"] if previous else "")
            self.conn.execute(
                """INSERT OR REPLACE INTO information_source_runtime
                (source_key,last_attempt_at,last_success_at,failure_count,next_retry_at,last_error,etag,last_modified)
                VALUES(?,?,?,?,?,?,?,?)""",
                (source_key[:200],now,last_success,failures,next_retry_at,"" if success else error[:500],
                 etag or old_etag,last_modified or old_modified),
            )
            self.conn.commit()

    async def cleanup_information(self, now: float) -> None:
        async with self._lock:
            with self._tx() as conn:
                conn.execute("DELETE FROM news_items WHERE expires_at<=?",(now,))
                conn.execute("DELETE FROM exploration_notes WHERE expires_at<=?",(now,))

    # 每个自然日只结算一次关系温度，避免巡检重复加分。
    async def update_relationships(self, day: str, day_start: float, day_end: float, now: float) -> None:
        async with self._lock:
            with self._tx() as conn:
                users=conn.execute("SELECT * FROM users WHERE enabled=1").fetchall()
                for user in users:
                    if user["last_relation_day"]==day: continue
                    msg_count=conn.execute(
                        "SELECT COUNT(*) FROM interaction_events WHERE user_id=? AND kind='message' AND created_at>=? AND created_at<?",
                        (user["user_id"],day_start,day_end),
                    ).fetchone()[0]
                    responded=conn.execute(
                        "SELECT 1 FROM interaction_events WHERE user_id=? AND kind='proactive_response' AND created_at>=? AND created_at<? LIMIT 1",
                        (user["user_id"],day_start,day_end),
                    ).fetchone()
                    delta=(0.5 if msg_count>0 else 0)+(0.5 if msg_count>=5 else 0)+(0.5 if responded else 0)
                    if msg_count==0 and user["last_user_message_at"] and now-user["last_user_message_at"]>7*86400:
                        delta=-0.25
                    temperature=max(20.0,min(100.0,float(user["temperature"])+delta))
                    conn.execute("UPDATE users SET temperature=?,last_relation_day=? WHERE user_id=?",(temperature,day,user["user_id"]))

    async def get_weather(self) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM weather_cache WHERE id=1").fetchone()
            if not row: return {}
            data=dict(row); data["raw_json"]=json.loads(data.get("raw_json") or "{}"); return data

    async def save_weather(self, data: dict[str, Any]) -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO weather_cache
                (id,fetched_at,location_name,latitude,longitude,temperature,weather_code,description,raw_json)
                VALUES(1,?,?,?,?,?,?,?,?)""",
                (data["fetched_at"],data["location_name"],data["latitude"],data["longitude"],
                 data.get("temperature"),data.get("weather_code"),data["description"],json.dumps(data.get("raw_json",{}),ensure_ascii=False)),
            ); self.conn.commit()

