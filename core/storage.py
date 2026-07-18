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

SCHEMA_VERSION = 9


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
            try:
                self._create_schema()
            except sqlite3.DatabaseError:
                # 结构无法兼容时保留原库，再用当前 Schema 建立新库。
                self._replace_incompatible_database()
                self._create_schema()

    def _replace_incompatible_database(self) -> None:
        if self._conn is not None:
            self._conn.close(); self._conn=None
        if self.path.exists():
            stamp=int(time.time()); backup=self.path.with_suffix(f".incompatible.{stamp}.db")
            counter=1
            while backup.exists():
                backup=self.path.with_suffix(f".incompatible.{stamp}.{counter}.db"); counter+=1
            shutil.move(str(self.path),str(backup))
        self._conn=sqlite3.connect(self.path,check_same_thread=False)
        self._conn.row_factory=sqlite3.Row

    # quick_check 失败时先保留损坏文件，再创建全新数据库。
    def _open_checked(self) -> None:
        """打开 SQLite 并执行完整性检查；损坏文件保留副本后再建立空库。"""
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            result = conn.execute("PRAGMA quick_check").fetchone()
            if result and str(result[0]).lower() != "ok":
                raise sqlite3.DatabaseError(str(result[0]))
            self._conn = conn
        except sqlite3.DatabaseError:
            try:
                if conn is not None:
                    conn.close()
                if self._conn is not None and self._conn is not conn:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None
            if self.path.exists():
                stamp=int(time.time()); backup = self.path.with_suffix(f".corrupt.{stamp}.db")
                counter=1
                while backup.exists():
                    backup=self.path.with_suffix(f".corrupt.{stamp}.{counter}.db"); counter+=1
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
        """幂等创建当前 Schema，并在建表完成后执行旧版本的增量迁移。"""
        version = 0
        existing = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if existing:
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            try:
                version = int(row[0]) if row else 0
            except (TypeError, ValueError) as exc:
                raise sqlite3.DatabaseError("schema_version 非法") from exc
            if version > SCHEMA_VERSION:
                self.conn.close()
                stamp=int(time.time()); backup = self.path.with_suffix(f".future-v{version}.{stamp}.db")
                counter=1
                while backup.exists():
                    backup=self.path.with_suffix(f".future-v{version}.{stamp}.{counter}.db"); counter+=1
                shutil.move(str(self.path), str(backup))
                self._conn = sqlite3.connect(self.path, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
        # 表按生活时间线、用户消息、长期记忆、社交与联网运行态分组；建表本身可重复执行。
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            -- 全局生活时间线、睡眠、梦境、日程和主动契机。
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
            -- 用户关系、互动、主动发送、休息积压和环境缓存。
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
              content_summary TEXT NOT NULL DEFAULT '', source_message_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_interaction_user_time ON interaction_events(user_id,created_at);
            CREATE TABLE IF NOT EXISTS proactive_events(
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, opportunity_id TEXT NOT NULL,
              stream_id TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
              expires_at REAL NOT NULL, sent_at REAL NOT NULL DEFAULT 0,
              host_task_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_proactive_pending ON proactive_events(stream_id,status,expires_at);
            CREATE TABLE IF NOT EXISTS rest_backlogs(
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
              created_at REAL NOT NULL, summary TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
              source_message_id TEXT NOT NULL DEFAULT ''
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
              created_at REAL NOT NULL, expires_at REAL NOT NULL, current_until REAL NOT NULL DEFAULT 0,
              source_message_ids TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_image_session_current ON image_summaries(session_id,current_until);
            -- 模型统计、回复防重和撤回关联使用短期运行记录。
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
            CREATE TABLE IF NOT EXISTS message_turn_sources(
              session_id TEXT NOT NULL, turn_anchor TEXT NOT NULL, source_message_id TEXT NOT NULL,
              user_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, expires_at REAL NOT NULL,
              PRIMARY KEY(session_id,turn_anchor,source_message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_message_turn_source
              ON message_turn_sources(session_id,source_message_id,expires_at);
            CREATE TABLE IF NOT EXISTS recall_events(
              session_id TEXT NOT NULL, recalled_message_id TEXT NOT NULL,
              user_id TEXT NOT NULL DEFAULT '', operator_id TEXT NOT NULL DEFAULT '',
              group_id TEXT NOT NULL DEFAULT '', notice_type TEXT NOT NULL,
              source_adapter TEXT NOT NULL DEFAULT 'unknown', summary TEXT NOT NULL DEFAULT '',
              summary_expires_at REAL NOT NULL DEFAULT 0,
              media_types TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, expires_at REAL NOT NULL,
              PRIMARY KEY(session_id,recalled_message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_recall_event_expiry
              ON recall_events(session_id,expires_at);
            -- 日记、重要日期和联网见闻只保存结构化摘要，不复制完整聊天历史。
            CREATE TABLE IF NOT EXISTS diary_entries(
              day TEXT PRIMARY KEY, created_at REAL NOT NULL, title TEXT NOT NULL,
              content TEXT NOT NULL, mood_summary TEXT NOT NULL,
              privacy TEXT NOT NULL DEFAULT 'private', source_digest TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS important_dates(
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
              event_name TEXT NOT NULL, event_date TEXT NOT NULL,
              recurrence TEXT NOT NULL DEFAULT 'none', source TEXT NOT NULL DEFAULT 'conversation',
              created_at REAL NOT NULL, updated_at REAL NOT NULL,
              source_message_id TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_important_date_unique
              ON important_dates(user_id,event_name,event_date,recurrence);
            CREATE INDEX IF NOT EXISTS idx_important_date_user ON important_dates(user_id,event_date);
            CREATE TABLE IF NOT EXISTS date_candidates(
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
              event_name TEXT NOT NULL, date_text TEXT NOT NULL,
              suggested_date TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 0,
              source_summary TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', source_message_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_date_candidate_user ON date_candidates(user_id,status,created_at);
            CREATE TABLE IF NOT EXISTS date_trigger_events(
              date_id INTEGER NOT NULL, occurrence_date TEXT NOT NULL,
              lead_days INTEGER NOT NULL, created_at REAL NOT NULL,
              PRIMARY KEY(date_id,occurrence_date,lead_days)
            );
            CREATE TABLE IF NOT EXISTS memory_runtime(
              id INTEGER PRIMARY KEY CHECK(id=1), last_diary_day TEXT NOT NULL DEFAULT '',
              last_cleanup_at REAL NOT NULL DEFAULT 0
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
            -- 群聊观察和跨会话转述使用 QQ 号定位，名称字段仅用于展示。
            CREATE TABLE IF NOT EXISTS group_observations(
              id TEXT PRIMARY KEY, group_id TEXT NOT NULL, group_alias TEXT NOT NULL,
              topic TEXT NOT NULL, summary TEXT NOT NULL, interest_score REAL NOT NULL DEFAULT 0,
              source_adapter TEXT NOT NULL DEFAULT 'unknown', created_at REAL NOT NULL,
              expires_at REAL NOT NULL, source_message_ids TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_group_observation_time
              ON group_observations(group_id,created_at);
            CREATE TABLE IF NOT EXISTS group_directory(
              group_id TEXT PRIMARY KEY, group_name TEXT NOT NULL DEFAULT '',
              stream_id TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS group_user_activity(
              group_id TEXT NOT NULL, user_id TEXT NOT NULL,
              display_name TEXT NOT NULL DEFAULT '', last_active_at REAL NOT NULL,
              source_message_id TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(group_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS relay_candidates(
              id TEXT PRIMARY KEY, kind TEXT NOT NULL,
              opportunity_id TEXT NOT NULL DEFAULT '',
              source_observation_id TEXT NOT NULL DEFAULT '', source_group_id TEXT NOT NULL DEFAULT '',
              target_group_id TEXT NOT NULL DEFAULT '', target_user_id TEXT NOT NULL DEFAULT '',
              target_stream_id TEXT NOT NULL, summary TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
              mention_user_id TEXT NOT NULL DEFAULT '', mention_name TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
              sent_at REAL NOT NULL DEFAULT 0, host_task_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_relay_pending
              ON relay_candidates(target_stream_id,status,expires_at);
            CREATE INDEX IF NOT EXISTS idx_relay_user_time
              ON relay_candidates(target_user_id,kind,created_at);
            CREATE TABLE IF NOT EXISTS relay_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, relay_id TEXT NOT NULL,
              status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
            );
            -- 书柜采用文档、不可变修订和运行记录三层结构，便于失败恢复与权限过滤。
            CREATE TABLE IF NOT EXISTS bookshelf_documents(
              id TEXT PRIMARY KEY, doc_type TEXT NOT NULL, work_type TEXT NOT NULL DEFAULT '',
              title TEXT NOT NULL, privacy TEXT NOT NULL, status TEXT NOT NULL,
              current_revision INTEGER NOT NULL DEFAULT 0,
              source_kind TEXT NOT NULL DEFAULT '', source_ref TEXT NOT NULL DEFAULT '',
              summary TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
              updated_at REAL NOT NULL, archived_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_bookshelf_visibility
              ON bookshelf_documents(doc_type,privacy,status,updated_at);
            CREATE TABLE IF NOT EXISTS bookshelf_revisions(
              id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL,
              revision_number INTEGER NOT NULL, stage TEXT NOT NULL,
              content TEXT NOT NULL, model_task TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
              UNIQUE(document_id,revision_number)
            );
            CREATE TABLE IF NOT EXISTS creation_inspirations(
              id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL,
              prompt_digest TEXT NOT NULL, privacy_ceiling TEXT NOT NULL,
              score REAL NOT NULL DEFAULT 0, status TEXT NOT NULL,
              created_at REAL NOT NULL, expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_creation_inspiration_pending
              ON creation_inspirations(status,score,created_at);
            CREATE TABLE IF NOT EXISTS creation_runs(
              id TEXT PRIMARY KEY, inspiration_id TEXT NOT NULL, document_id TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL, started_at REAL NOT NULL, completed_at REAL NOT NULL DEFAULT 0,
              error_summary TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS reading_notes(
              id TEXT PRIMARY KEY, source_api TEXT NOT NULL, external_id TEXT NOT NULL,
              title TEXT NOT NULL, summary TEXT NOT NULL, annotation TEXT NOT NULL,
              source_digest TEXT NOT NULL, created_at REAL NOT NULL,
              UNIQUE(source_api,external_id)
            );
            -- 搜索 Key 只保存不可逆指纹和健康状态，调用事件与模型 Token 分开统计。
            CREATE TABLE IF NOT EXISTS search_key_runtime(
              provider_id TEXT NOT NULL, key_fingerprint TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'healthy', cooldown_until REAL NOT NULL DEFAULT 0,
              failure_count INTEGER NOT NULL DEFAULT 0, last_error_class TEXT NOT NULL DEFAULT '',
              last_used_at REAL NOT NULL DEFAULT 0, last_success_at REAL NOT NULL DEFAULT 0,
              PRIMARY KEY(provider_id,key_fingerprint)
            );
            CREATE TABLE IF NOT EXISTS search_api_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
              operation TEXT NOT NULL, provider_id TEXT NOT NULL, provider_type TEXT NOT NULL,
              key_fingerprint TEXT NOT NULL DEFAULT '', success INTEGER NOT NULL,
              status_code INTEGER NOT NULL DEFAULT 0, latency_ms REAL NOT NULL DEFAULT 0,
              result_count INTEGER NOT NULL DEFAULT 0, error_class TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_search_api_event_time
              ON search_api_events(created_at,provider_type,success);
            """
        )
        # v1 数据库可能已经存在 users 表；只补列，不重建用户数据。
        self._ensure_column("users", "role", "TEXT NOT NULL DEFAULT 'friend'")
        self._ensure_column("users", "daily_proactive_max", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("proactive_opportunities", "target_user_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("relay_candidates", "opportunity_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("proactive_events", "host_task_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("relay_candidates", "host_task_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("interaction_events", "source_message_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("rest_backlogs", "source_message_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("image_summaries", "source_message_ids", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("important_dates", "source_message_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("date_candidates", "source_message_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("group_observations", "source_message_ids", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("recall_events", "summary_expires_at", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("group_user_activity", "source_message_id", "TEXT NOT NULL DEFAULT ''")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_proactive_host_task ON proactive_events(host_task_id,status)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_relay_host_task ON relay_candidates(host_task_id,status)"
        )
        # v1.5 首次升级时把已有私人日记纳入书柜，不复制聊天记录，也不改变原日记表。
        self.conn.execute(
            """INSERT OR IGNORE INTO bookshelf_documents
            (id,doc_type,work_type,title,privacy,status,current_revision,source_kind,source_ref,summary,
             created_at,updated_at,archived_at)
            SELECT 'diary:'||day,'diary','',title,'private','archived',1,'diary',day,mood_summary,
                   created_at,created_at,created_at FROM diary_entries"""
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO bookshelf_revisions
            (document_id,revision_number,stage,content,model_task,created_at)
            SELECT 'diary:'||day,1,'diary',content,'diary',created_at FROM diary_entries"""
        )
        if version < 9:
            self.conn.commit()
            self._migrate_to_v9()
        self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",(str(SCHEMA_VERSION),))
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

    def _migrate_to_v9(self) -> None:
        """删除已取消的技能/别名数据，同时保留其余生活状态。"""
        with self._tx() as conn:
            conn.execute("DROP TABLE IF EXISTS skill_events")
            conn.execute("DROP TABLE IF EXISTS skills")
            conn.execute("DROP TABLE IF EXISTS relationship_entries")
            # v1.6 的 -1 表示“继承角色默认值”；v1.7 起数据库中只保存明确额度。
            conn.execute("""UPDATE users SET daily_proactive_max=
                CASE WHEN role='owner' THEN 2 ELSE 1 END WHERE daily_proactive_max<0""")
            # 旧版允许手填昵称、群别名和 @ 目标；升级后清空这些展示快照，等待 Host 重新读取。
            conn.execute("UPDATE users SET display_name='' ")
            conn.execute("UPDATE group_observations SET group_alias='' ")
            conn.execute("UPDATE relay_candidates SET mention_user_id='',mention_name='' ")
            columns={str(row[1]) for row in conn.execute("PRAGMA table_info(memory_runtime)").fetchall()}
            if "last_skill_day" in columns:
                conn.execute("""CREATE TABLE memory_runtime_v9(
                    id INTEGER PRIMARY KEY CHECK(id=1), last_diary_day TEXT NOT NULL DEFAULT '',
                    last_cleanup_at REAL NOT NULL DEFAULT 0)""")
                conn.execute("""INSERT OR REPLACE INTO memory_runtime_v9(id,last_diary_day,last_cleanup_at)
                    SELECT id,last_diary_day,last_cleanup_at FROM memory_runtime""")
                conn.execute("DROP TABLE memory_runtime")
                conn.execute("ALTER TABLE memory_runtime_v9 RENAME TO memory_runtime")

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
                        sleep_started_at: float, fragments: list[str] | None = None,
                        created_at: float = 0) -> int:
        async with self._lock:
            with self._tx() as conn:
                cursor=conn.execute(
                    "INSERT INTO dreams(created_at,content,mood_delta,energy_delta,sleep_started_at) VALUES(?,?,?,?,?)",
                    (created_at or time.time(), content[:1000], mood_delta, energy_delta, sleep_started_at),
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
        """将 WebUI 用户列表同步到运行库；QQ 号与角色配置是身份判断的唯一来源。"""
        async with self._lock:
            with self._tx() as conn:
                seen=[]
                for p in profiles:
                    uid=str(p.user_id).strip()
                    if not uid: continue
                    seen.append(uid)
                    role=str(getattr(p,"role","friend") or "friend")
                    daily_limit=max(0,min(20,int(getattr(p,"daily_proactive_max",1))))
                    conn.execute(
                        """INSERT INTO users(user_id,enabled,proactive_enabled,display_name,temperature,role,daily_proactive_max,quiet_start,quiet_end)
                        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                        enabled=excluded.enabled,proactive_enabled=excluded.proactive_enabled,
                        role=excluded.role,daily_proactive_max=excluded.daily_proactive_max,
                        quiet_start=excluded.quiet_start,quiet_end=excluded.quiet_end""",
                        (uid,int(p.enabled),int(p.proactive_enabled),"",float(p.initial_temperature),role,daily_limit,p.quiet_start,p.quiet_end),
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

    async def list_users(self, proactive_only: bool=False, *, include_disabled:bool=False) -> list[dict[str, Any]]:
        async with self._lock:
            sql="SELECT * FROM users" if include_disabled else "SELECT * FROM users WHERE enabled=1"
            if proactive_only:sql += " WHERE enabled=1 AND proactive_enabled=1" if include_disabled else " AND proactive_enabled=1"
            return [dict(r) for r in self.conn.execute(sql).fetchall()]

    async def set_user_stream(self, user_id: str, stream_id: str) -> None:
        async with self._lock:
            self.conn.execute("UPDATE users SET stream_id=? WHERE user_id=?",(stream_id,user_id)); self.conn.commit()

    async def update_user_display_name(self,user_id:str,display_name:str)->None:
        """昵称只作为自动展示快照，身份与授权始终使用 user_id。"""
        clean=" ".join(str(display_name or "").replace("\x00","").split())[:120]
        if not user_id or not clean:return
        async with self._lock:
            self.conn.execute("UPDATE users SET display_name=? WHERE user_id=?",(clean,user_id)); self.conn.commit()

    # 同时记录活跃时段，并识别用户是否回应了近期主动消息。
    async def record_interaction(self, user_id: str, text: str, now: float, hour: int,
                                 source_message_id: str="") -> None:
        """记录截断后的互动摘要，并原子更新用户活跃时间和主动回复标记。"""
        async with self._lock:
            with self._tx() as conn:
                conn.execute("UPDATE users SET last_user_message_at=? WHERE user_id=?",(now,user_id))
                conn.execute(
                    """INSERT INTO interaction_events
                    (user_id,created_at,kind,hour,content_summary,source_message_id) VALUES(?,?,?,?,?,?)""",
                    (user_id,now,"message",hour,text[:240],source_message_id[:240]),
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
                            """INSERT INTO interaction_events
                            (user_id,created_at,kind,hour,content_summary,source_message_id) VALUES(?,?,?,?,?,?)""",
                            (user_id,now,"proactive_response",hour,"回应了主动消息",source_message_id[:240]),
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

    async def add_rest_backlog(self, user_id: str, summary: str, now: float,
                               source_message_id: str="") -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT INTO rest_backlogs
                (user_id,created_at,summary,source_message_id) VALUES(?,?,?,?)""",
                (user_id,now,summary[:240],source_message_id[:240]),
            ); self.conn.commit()

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

    async def set_proactive_task_id(self, event_id: str, host_task_id: str) -> bool:
        """关联 Host 主动任务，后续只确认该任务实际产生的发送。"""
        if not event_id or not host_task_id:return False
        async with self._lock:
            cursor=self.conn.execute(
                "UPDATE proactive_events SET host_task_id=? WHERE id=? AND status='pending'",
                (host_task_id[:240],event_id),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def set_proactive_event_status(self, event_id: str, status: str) -> None:
        async with self._lock:
            self.conn.execute(
                "UPDATE proactive_events SET status=? WHERE id=? AND status='pending'",
                (status[:24],event_id),
            )
            self.conn.commit()

    async def pending_proactive_for_task(self, stream_id: str, host_task_id: str, now: float) -> dict[str, Any]:
        if not stream_id or not host_task_id:return {}
        async with self._lock:
            row=self.conn.execute(
                """SELECT id,user_id,opportunity_id,stream_id,status,created_at,expires_at,sent_at,host_task_id
                FROM proactive_events WHERE stream_id=? AND host_task_id=? AND status='pending' AND expires_at>?""",
                (stream_id,host_task_id,now),
            ).fetchone()
            return dict(row) if row else {}

    async def proactive_for_task(self, stream_id: str, host_task_id: str) -> dict[str, Any]:
        if not stream_id or not host_task_id:return {}
        async with self._lock:
            row=self.conn.execute(
                """SELECT id,user_id,opportunity_id,stream_id,status,created_at,expires_at,sent_at,host_task_id
                FROM proactive_events WHERE stream_id=? AND host_task_id=?""",
                (stream_id,host_task_id),
            ).fetchone()
            return dict(row) if row else {}

    async def proactive_event(self, event_id: str) -> dict[str, Any]:
        """按插件事件号读取记录，用于消除 Host 唤醒与 task_id 回写之间的竞态。"""
        if not event_id:return {}
        async with self._lock:
            row=self.conn.execute(
                """SELECT id,user_id,opportunity_id,stream_id,status,created_at,expires_at,sent_at,host_task_id
                FROM proactive_events WHERE id=?""",
                (event_id,),
            ).fetchone()
            return dict(row) if row else {}

    # 只有 send_service.after_send 确认平台发送成功后，才增加实际主动额度。
    async def mark_pending_sent(self, stream_id: str, now: float, day: str = "", *, event_id: str = "") -> bool:
        """提交已成功发送的主动候选；精确 event_id 优先于旧版会话兜底。"""
        async with self._lock:
            # 普通被动回复没有 pending 记录，只读查询不会创建 SQLite journal。
            if event_id:
                row=self.conn.execute(
                    """SELECT id,user_id,opportunity_id FROM proactive_events
                    WHERE id=? AND stream_id=? AND status='pending'""",
                    (event_id,stream_id),
                ).fetchone()
            else:
                # 保留 v1 API 的会话兜底；插件主链使用 event_id 做精确确认。
                row=self.conn.execute(
                    "SELECT id,user_id,opportunity_id FROM proactive_events WHERE stream_id=? AND status='pending' AND expires_at>? ORDER BY created_at DESC LIMIT 1",
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
                relay=conn.execute(
                    "SELECT id FROM relay_candidates WHERE opportunity_id=? AND kind='group_to_private' AND status='queued'",
                    (row[2],),
                ).fetchone()
                if relay:
                    conn.execute("UPDATE relay_candidates SET status='sent',sent_at=? WHERE id=?",(now,relay[0]))
                    conn.execute("INSERT INTO relay_events(relay_id,status,detail,created_at) VALUES(?,?,?,?)",
                                 (relay[0],"sent","proactive_after_send",now))
                return True

    async def expire_pending(self, now: float) -> None:
        async with self._lock:
            self.conn.execute("UPDATE proactive_events SET status='expired' WHERE status='pending' AND expires_at<=?",(now,)); self.conn.commit()

    async def pending_proactive_users(self, now: float) -> set[str]:
        """返回仍在等待 Planner/平台确认的用户，防止额度在发送前被并发穿透。"""
        async with self._lock:
            rows=self.conn.execute(
                "SELECT DISTINCT user_id FROM proactive_events WHERE status='pending' AND expires_at>?",
                (now,),
            ).fetchall()
            return {str(row[0]) for row in rows if str(row[0] or "")}

    async def set_wake_candidate(self, session_id: str, user_id: str, message_id: str, reason: str, now: float, expires_at: float) -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO wake_candidates
                (session_id,user_id,message_id,reason,created_at,expires_at) VALUES(?,?,?,?,?,?)""",
                (session_id,user_id,message_id,reason[:240],now,expires_at),
            )
            self.conn.commit()

    async def pop_wake_candidate(self, session_id: str, now: float, message_id: str = "") -> dict[str, Any]:
        """只有真实发送时消费待醒候选；普通发送只执行一次只读查询。"""
        async with self._lock:
            if message_id:
                row=self.conn.execute(
                    "SELECT * FROM wake_candidates WHERE session_id=? AND message_id=? AND expires_at>?",
                    (session_id,message_id,now),
                ).fetchone()
            else:
                row=self.conn.execute(
                    "SELECT * FROM wake_candidates WHERE session_id=? AND expires_at>?",
                    (session_id,now),
                ).fetchone()
            if not row:
                self.conn.execute("DELETE FROM wake_candidates WHERE session_id=? AND expires_at<=?",(session_id,now))
                self.conn.commit()
                return {}
            with self._tx() as conn:
                conn.execute("DELETE FROM wake_candidates WHERE session_id=? AND message_id=?",(session_id,row["message_id"]))
            return dict(row)

    async def clear_wake_candidate(self, session_id: str, message_id: str = "") -> None:
        async with self._lock:
            if message_id:
                self.conn.execute("DELETE FROM wake_candidates WHERE session_id=? AND message_id=?",(session_id,message_id))
            else:
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
                                 session_id: str, now: float, expires_at: float, current_until: float,
                                 source_message_ids: list[str]|None=None) -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO image_summaries
                (image_hash,summary,source_type,ownership_hint,session_id,created_at,expires_at,current_until,source_message_ids)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (image_hash,summary[:1000],source_type[:40],ownership_hint[:240],session_id,now,expires_at,current_until,
                 json.dumps([str(item)[:240] for item in (source_message_ids or []) if str(item).strip()],ensure_ascii=False)),
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

    async def register_message_turn(self, session_id: str, turn_anchor: str, source_message_ids: list[str],
                                    user_id: str, now: float, expires_at: float) -> None:
        """持久化合并消息与最终回复锚点，热重载后仍能识别撤回轮次。"""
        sources=[]
        for value in source_message_ids:
            normalized=str(value or "").strip()[:240]
            if normalized and normalized not in sources:sources.append(normalized)
        if not session_id or not turn_anchor or not sources:return
        async with self._lock:
            with self._tx() as conn:
                conn.execute("DELETE FROM message_turn_sources WHERE expires_at<=?",(now,))
                conn.executemany(
                    """INSERT OR REPLACE INTO message_turn_sources
                    (session_id,turn_anchor,source_message_id,user_id,created_at,expires_at)
                    VALUES(?,?,?,?,?,?)""",
                    [(session_id[:240],turn_anchor[:240],source,user_id[:80],now,expires_at) for source in sources],
                )

    async def record_recall_event(self, *, session_id: str, recalled_message_id: str, user_id: str,
                                  operator_id: str, group_id: str, notice_type: str,
                                  source_adapter: str, summary: str, media: list[str],
                                  now: float, expires_at: float, summary_expires_at: float=0) -> None:
        """写入撤回墓碑和可选短摘要；同一消息重复通知时以最新安全字段覆盖。"""
        if not session_id or not recalled_message_id:return
        async with self._lock:
            self.conn.execute(
                """INSERT INTO recall_events
                (session_id,recalled_message_id,user_id,operator_id,group_id,notice_type,source_adapter,
                 summary,summary_expires_at,media_types,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id,recalled_message_id) DO UPDATE SET
                user_id=excluded.user_id,operator_id=excluded.operator_id,group_id=excluded.group_id,
                notice_type=excluded.notice_type,source_adapter=excluded.source_adapter,
                summary=CASE WHEN excluded.summary<>'' OR excluded.media_types<>'[]' THEN excluded.summary ELSE recall_events.summary END,
                summary_expires_at=CASE WHEN excluded.summary<>'' OR excluded.media_types<>'[]' THEN excluded.summary_expires_at ELSE recall_events.summary_expires_at END,
                media_types=CASE WHEN excluded.media_types<>'[]' THEN excluded.media_types ELSE recall_events.media_types END,
                created_at=excluded.created_at,expires_at=MAX(recall_events.expires_at,excluded.expires_at)""",
                (session_id[:240],recalled_message_id[:240],user_id[:80],operator_id[:80],group_id[:80],
                 notice_type[:40],source_adapter[:32],summary[:1000],summary_expires_at,
                 json.dumps(media,ensure_ascii=False),now,expires_at),
            )
            self.conn.commit()

    async def is_recalled_turn(self, session_id: str, turn_anchor: str, now: float) -> bool:
        """直接锚点或其任一合并来源被撤回时，整轮都视为已取消。"""
        if not session_id or not turn_anchor:return False
        async with self._lock:
            row=self.conn.execute(
                """SELECT 1 FROM recall_events r
                WHERE r.session_id=? AND r.expires_at>? AND (
                  r.recalled_message_id=? OR EXISTS(
                    SELECT 1 FROM message_turn_sources t
                    WHERE t.session_id=r.session_id AND t.turn_anchor=?
                    AND t.source_message_id=r.recalled_message_id AND t.expires_at>?
                  )
                ) LIMIT 1""",
                (session_id,now,turn_anchor,turn_anchor,now),
            ).fetchone()
            return row is not None

    async def turn_anchors_for_source(self, session_id: str, source_message_id: str, now: float) -> list[str]:
        if not session_id or not source_message_id:return []
        async with self._lock:
            rows=self.conn.execute(
                """SELECT DISTINCT turn_anchor FROM message_turn_sources
                WHERE session_id=? AND source_message_id=? AND expires_at>?""",
                (session_id,source_message_id,now),
            ).fetchall()
            anchors=[str(row[0]) for row in rows if str(row[0]).strip()]
            if source_message_id not in anchors:anchors.append(source_message_id)
            return anchors

    async def recent_recall_context(self, session_id: str, now: float, limit: int=5) -> list[dict[str,Any]]:
        if not session_id:return []
        async with self._lock:
            rows=self.conn.execute(
                """SELECT recalled_message_id,notice_type,created_at FROM recall_events
                WHERE session_id=? AND expires_at>? ORDER BY created_at DESC LIMIT ?""",
                (session_id,now,max(1,min(20,int(limit)))),
            ).fetchall()
            return [dict(row) for row in rows]

    async def latest_recall_summary(self, session_id: str, user_id: str, now: float) -> dict[str,Any]:
        if not session_id or not user_id:return {}
        async with self._lock:
            row=self.conn.execute(
                """SELECT recalled_message_id,summary,media_types,created_at FROM recall_events
                WHERE session_id=? AND user_id=? AND group_id='' AND (summary<>'' OR media_types<>'[]')
                AND summary_expires_at>?
                ORDER BY created_at DESC LIMIT 1""",
                (session_id,user_id,now),
            ).fetchone()
            if not row:return {}
            result=dict(row)
            try:result["media_types"]=json.loads(result.get("media_types") or "[]")
            except (TypeError,json.JSONDecodeError):result["media_types"]=[]
            return result

    async def clear_recall_summaries(self) -> None:
        """关闭摘要缓存后立即清空正文与媒介元数据，但保留发送取消墓碑。"""
        async with self._lock:
            self.conn.execute(
                "UPDATE recall_events SET summary='',media_types='[]',summary_expires_at=0 "
                "WHERE summary<>'' OR media_types<>'[]' OR summary_expires_at<>0"
            )
            self.conn.commit()

    async def redact_recalled_private_artifacts(self, user_id: str, message_id: str) -> None:
        """撤回后移除由该私聊消息衍生的短期状态，避免后续 Prompt 再次引用。"""
        if not user_id or not message_id:return
        async with self._lock:
            with self._tx() as conn:
                conn.execute("DELETE FROM interaction_events WHERE user_id=? AND source_message_id=?",(user_id,message_id))
                latest=conn.execute(
                    "SELECT COALESCE(MAX(created_at),0) FROM interaction_events WHERE user_id=? AND kind='message'",
                    (user_id,),
                ).fetchone()
                conn.execute("UPDATE users SET last_user_message_at=? WHERE user_id=?",(float(latest[0] or 0),user_id))
                conn.execute("DELETE FROM rest_backlogs WHERE user_id=? AND source_message_id=?",(user_id,message_id))
                conn.execute("DELETE FROM date_candidates WHERE user_id=? AND source_message_id=? AND status='pending'",(user_id,message_id))
                date_rows=conn.execute(
                    "SELECT id FROM important_dates WHERE user_id=? AND source_message_id=?",(user_id,message_id)
                ).fetchall()
                for row in date_rows:
                    date_id=int(row[0])
                    conn.execute("DELETE FROM date_trigger_events WHERE date_id=?",(date_id,))
                    opportunity_rows=conn.execute(
                        "SELECT id FROM proactive_opportunities WHERE framework_id=?",
                        (f"important-date:{date_id}",),
                    ).fetchall()
                    for opportunity in opportunity_rows:
                        conn.execute(
                            "UPDATE proactive_events SET status='cancelled' WHERE opportunity_id=? AND status='pending'",
                            (opportunity[0],),
                        )
                    conn.execute("DELETE FROM proactive_opportunities WHERE framework_id=?",
                                 (f"important-date:{date_id}",))
                conn.execute("DELETE FROM important_dates WHERE user_id=? AND source_message_id=?",(user_id,message_id))
                image_rows=conn.execute(
                    "SELECT image_hash,source_message_ids FROM image_summaries"
                ).fetchall()
                remove_hashes=[]
                for row in image_rows:
                    try:sources=json.loads(row[1] or "[]")
                    except (TypeError,json.JSONDecodeError):sources=[]
                    if message_id in {str(item) for item in sources}:remove_hashes.append(str(row[0]))
                if remove_hashes:
                    conn.executemany("DELETE FROM image_summaries WHERE image_hash=?",[(value,) for value in remove_hashes])

    async def retract_group_observation_source(self, group_id: str, message_id: str, now: float) -> int:
        """群消息撤回后删除包含该来源的匿名摘要，并取消尚未发送的群转私候选。"""
        if not group_id or not message_id:return 0
        async with self._lock:
            rows=self.conn.execute(
                "SELECT id,source_message_ids FROM group_observations WHERE group_id=? AND expires_at>?",
                (group_id,now),
            ).fetchall()
            observation_ids=[]
            for row in rows:
                try:sources=json.loads(row[1] or "[]")
                except (TypeError,json.JSONDecodeError):sources=[]
                if message_id in {str(item) for item in sources}:observation_ids.append(str(row[0]))
            if not observation_ids:return 0
            with self._tx() as conn:
                for observation_id in observation_ids:
                    relays=conn.execute(
                        """SELECT id,opportunity_id FROM relay_candidates
                        WHERE source_observation_id=? AND kind='group_to_private'""",(observation_id,)
                    ).fetchall()
                    for relay in relays:
                        opportunity_id=str(relay[1] or "")
                        conn.execute(
                            """UPDATE relay_candidates SET status='cancelled'
                            WHERE id=? AND status IN ('queued','pending','sending')""",(relay[0],)
                        )
                        if opportunity_id:
                            conn.execute(
                                "UPDATE proactive_events SET status='cancelled' WHERE opportunity_id=? AND status='pending'",
                                (opportunity_id,),
                            )
                            conn.execute("DELETE FROM proactive_opportunities WHERE id=?",(opportunity_id,))
                    conn.execute("DELETE FROM group_observations WHERE id=?",(observation_id,))
            return len(observation_ids)

    async def record_group_activity(self, group_id: str, user_id: str, display_name: str, now: float,
                                    source_message_id: str="") -> None:
        if not group_id or not user_id:return
        async with self._lock:
            self.conn.execute(
                """INSERT INTO group_user_activity(group_id,user_id,display_name,last_active_at,source_message_id)
                VALUES(?,?,?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET
                display_name=CASE WHEN excluded.last_active_at>=group_user_activity.last_active_at
                                  THEN excluded.display_name ELSE group_user_activity.display_name END,
                source_message_id=CASE WHEN excluded.last_active_at>=group_user_activity.last_active_at
                                       THEN excluded.source_message_id ELSE group_user_activity.source_message_id END,
                last_active_at=MAX(group_user_activity.last_active_at,excluded.last_active_at)""",
                (group_id,user_id,display_name[:120],now,source_message_id[:240]),
            )
            self.conn.commit()

    async def upsert_group_directory(self,group_id:str,group_name:str,stream_id:str,now:float)->None:
        if not group_id:return
        name=" ".join(str(group_name or "").replace("\x00","").split())[:160]
        async with self._lock:
            self.conn.execute(
                """INSERT INTO group_directory(group_id,group_name,stream_id,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(group_id) DO UPDATE SET
                group_name=CASE WHEN excluded.group_name!='' THEN excluded.group_name ELSE group_directory.group_name END,
                stream_id=CASE WHEN excluded.stream_id!='' THEN excluded.stream_id ELSE group_directory.stream_id END,
                updated_at=MAX(group_directory.updated_at,excluded.updated_at)""",
                (group_id,name,str(stream_id or "")[:240],float(now)),
            ); self.conn.commit()

    async def get_group_directory(self,group_id:str)->dict[str,Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM group_directory WHERE group_id=?",(group_id,)).fetchone()
            return dict(row) if row else {}

    async def list_group_directory(self,limit:int=100)->list[dict[str,Any]]:
        async with self._lock:
            rows=self.conn.execute(
                "SELECT * FROM group_directory ORDER BY updated_at DESC LIMIT ?",(max(1,min(500,int(limit))),)
            ).fetchall()
            return [dict(row) for row in rows]

    async def unique_group_for_stream(self,stream_id:str)->str:
        async with self._lock:
            rows=self.conn.execute(
                "SELECT group_id FROM group_directory WHERE stream_id=? ORDER BY updated_at DESC",(stream_id,)
            ).fetchall()
            values={str(row[0]) for row in rows if str(row[0])}
            return next(iter(values)) if len(values)==1 else ""

    async def clear_recalled_group_activity(self, group_id: str, message_id: str) -> int:
        """仅清除仍由该撤回消息占据的最新活跃指针；未知状态不会触发群转私。"""
        if not group_id or not message_id:return 0
        async with self._lock:
            cursor=self.conn.execute(
                """UPDATE group_user_activity SET display_name='',last_active_at=0,source_message_id=''
                WHERE group_id=? AND source_message_id=?""",(group_id,message_id),
            )
            self.conn.commit(); return int(cursor.rowcount)

    async def get_group_activity(self, group_id: str, user_id: str) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute(
                "SELECT * FROM group_user_activity WHERE group_id=? AND user_id=?",(group_id,user_id)
            ).fetchone()
            return dict(row) if row else {}

    async def save_group_observation(self, item: dict[str, Any]) -> bool:
        """群聊原文不会传入存储层，只接受长度受限的公开话题摘要。"""
        async with self._lock:
            cursor=self.conn.execute(
                """INSERT OR IGNORE INTO group_observations
                (id,group_id,group_alias,topic,summary,interest_score,source_adapter,created_at,expires_at,source_message_ids)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item["id"],item["group_id"],item["group_alias"],item["topic"][:240],item["summary"][:1200],
                 max(0,min(1,float(item.get("interest_score") or 0))),item.get("source_adapter","unknown")[:32],
                 float(item["created_at"]),float(item["expires_at"]),
                 json.dumps([str(value)[:240] for value in item.get("source_message_ids") or [] if str(value).strip()],ensure_ascii=False)),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def recent_group_observations(self, now: float, limit: int=10) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT * FROM group_observations WHERE expires_at>?
                ORDER BY created_at DESC LIMIT ?""",(now,max(1,min(100,int(limit))))
            ).fetchall()
            return [dict(row) for row in rows]

    async def create_relay_candidate(self, item: dict[str, Any]) -> bool:
        """创建转述候选，并在同一事务内把相同目标的旧候选标记为已取代。"""
        async with self._lock:
            with self._tx() as conn:
                if item["kind"]=="explicit":
                    sending=conn.execute(
                        """SELECT 1 FROM relay_candidates WHERE target_stream_id=? AND kind='explicit'
                        AND status='sending' AND expires_at>? LIMIT 1""",
                        (item.get("target_stream_id",""),float(item["created_at"])),
                    ).fetchone()
                    if sending:return False
                    stale=conn.execute(
                        """SELECT id FROM relay_candidates WHERE target_stream_id=? AND kind='explicit'
                        AND status='pending' AND expires_at>?""",
                        (item.get("target_stream_id",""),float(item["created_at"])),
                    ).fetchall()
                    conn.execute(
                        """UPDATE relay_candidates SET status='superseded' WHERE target_stream_id=?
                        AND kind='explicit' AND status='pending'""",(item.get("target_stream_id",""),)
                    )
                    conn.executemany(
                        "INSERT INTO relay_events(relay_id,status,detail,created_at) VALUES(?,'superseded','newer_relay',?)",
                        [(row[0],float(item["created_at"])) for row in stale],
                    )
                cursor=conn.execute(
                    """INSERT OR IGNORE INTO relay_candidates
                    (id,kind,opportunity_id,source_observation_id,source_group_id,target_group_id,target_user_id,target_stream_id,
                     summary,reason,mention_user_id,mention_name,status,created_at,expires_at,host_task_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item["id"],item["kind"],item.get("opportunity_id","")[:80],item.get("source_observation_id","")[:80],item.get("source_group_id","")[:80],
                     item.get("target_group_id","")[:80],item.get("target_user_id","")[:80],item.get("target_stream_id","")[:160],
                     item.get("summary","")[:1200],item.get("reason","")[:1200],item.get("mention_user_id","")[:80],
                     item.get("mention_name","")[:120],item.get("status","pending")[:24],float(item["created_at"]),
                     float(item["expires_at"]),item.get("host_task_id","")[:240]),
                )
                if cursor.rowcount:
                    conn.execute("INSERT INTO relay_events(relay_id,status,detail,created_at) VALUES(?,?,?,?)",
                                 (item["id"],item.get("status","pending"),"created",float(item["created_at"])))
                return cursor.rowcount==1

    async def pending_relay_context(self, stream_id: str, now: float) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute(
                """SELECT * FROM relay_candidates WHERE target_stream_id=? AND kind='explicit'
                AND status IN ('pending','sending') AND expires_at>?
                ORDER BY created_at DESC LIMIT 1""",(stream_id,now)
            ).fetchone()
            return dict(row) if row else {}

    async def set_relay_task_id(self, relay_id: str, host_task_id: str) -> bool:
        if not relay_id or not host_task_id:return False
        async with self._lock:
            cursor=self.conn.execute(
                "UPDATE relay_candidates SET host_task_id=? WHERE id=? AND status='pending'",
                (host_task_id[:240],relay_id),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def relay_for_task(self, stream_id: str, host_task_id: str) -> dict[str, Any]:
        if not stream_id or not host_task_id:return {}
        async with self._lock:
            row=self.conn.execute(
                "SELECT * FROM relay_candidates WHERE target_stream_id=? AND host_task_id=? AND kind='explicit'",
                (stream_id,host_task_id),
            ).fetchone()
            return dict(row) if row else {}

    async def relay_candidate(self, relay_id: str) -> dict[str, Any]:
        """按候选号读取显式转述，支持 Planner 先于 Host task_id 回写到达。"""
        if not relay_id:return {}
        async with self._lock:
            row=self.conn.execute(
                "SELECT * FROM relay_candidates WHERE id=? AND kind='explicit'",
                (relay_id,),
            ).fetchone()
            return dict(row) if row else {}

    async def reserve_relay_for_send(self, stream_id: str, now: float, host_task_id: str = "") -> dict[str, Any]:
        """发送前原子占用一次 @；Host 正常分段时只有第一段会命中。"""
        async with self._lock:
            with self._tx() as conn:
                if host_task_id:
                    row=conn.execute(
                        """SELECT * FROM relay_candidates WHERE target_stream_id=? AND host_task_id=?
                        AND kind='explicit' AND status='pending' AND expires_at>?""",
                        (stream_id,host_task_id,now),
                    ).fetchone()
                else:
                    # 兼容不返回 task_id 的旧 Host；不抢占已经精确关联的新任务。
                    row=conn.execute(
                        """SELECT * FROM relay_candidates WHERE target_stream_id=? AND kind='explicit'
                        AND host_task_id='' AND status='pending' AND expires_at>?
                        ORDER BY created_at DESC LIMIT 1""",(stream_id,now)
                    ).fetchone()
                if not row:return {}
                changed=conn.execute("UPDATE relay_candidates SET status='sending' WHERE id=? AND status='pending'",
                                     (row["id"],)).rowcount
                if not changed:return {}
                conn.execute("INSERT INTO relay_events(relay_id,status,detail,created_at) VALUES(?,?,?,?)",
                             (row["id"],"sending","before_send",now))
                return dict(row)

    async def finish_relay_send(self, relay_id: str, sent: bool, now: float) -> None:
        async with self._lock:
            with self._tx() as conn:
                row=conn.execute("SELECT status,expires_at FROM relay_candidates WHERE id=?",(relay_id,)).fetchone()
                if not row:return
                current=str(row[0] or "")
                if current=="sent":return
                # 已被禁用、过期或新任务取代的在途发送若失败，保留原终态；若平台实际成功则记录事实。
                if not sent and current!="sending":return
                status="sent" if sent else ("pending" if float(row[1])>now else "expired")
                conn.execute("UPDATE relay_candidates SET status=?,sent_at=? WHERE id=?",
                             (status,now if sent else 0,relay_id))
                conn.execute("INSERT INTO relay_events(relay_id,status,detail,created_at) VALUES(?,?,?,?)",
                             (relay_id,status,"after_send",now))

    async def set_relay_status(self, relay_id: str, status: str, now: float, detail: str="") -> None:
        """只从非终态迁移；已 sent/superseded/failed/expired/cancelled 的候选不会被覆盖。"""
        async with self._lock:
            with self._tx() as conn:
                cursor=conn.execute(
                    "UPDATE relay_candidates SET status=? WHERE id=? AND status IN ('pending','sending','queued')",
                    (status[:24],relay_id),
                )
                if cursor.rowcount:
                    conn.execute("INSERT INTO relay_events(relay_id,status,detail,created_at) VALUES(?,?,?,?)",
                                 (relay_id,status[:24],detail[:300],now))

    async def social_share_stats(self, user_id: str, since: float) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute(
                """SELECT COUNT(*) count,MAX(created_at) last_at FROM relay_candidates
                WHERE target_user_id=? AND kind='group_to_private' AND created_at>=?
                AND status IN ('queued','sent')""",(user_id,since)
            ).fetchone()
            return {"count":int(row[0] or 0),"last_at":float(row[1] or 0)} if row else {"count":0,"last_at":0}

    async def add_creation_inspiration(self, item: dict[str, Any]) -> bool:
        async with self._lock:
            cursor=self.conn.execute(
                """INSERT OR IGNORE INTO creation_inspirations
                (id,source_kind,source_ref,prompt_digest,privacy_ceiling,score,status,created_at,expires_at)
                VALUES(?,?,?,?,?,?, 'pending',?,?)""",
                (item["id"],item["source_kind"][:40],item["source_ref"][:160],item["prompt_digest"][:2000],
                 "public" if item.get("privacy_ceiling")=="public" else "private",
                 max(0,min(1,float(item.get("score") or 0))),float(item["created_at"]),float(item["expires_at"])),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def pending_creation_inspirations(self, now: float, limit: int=10) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT * FROM creation_inspirations WHERE status='pending' AND expires_at>?
                ORDER BY score DESC,created_at DESC LIMIT ?""",(now,max(1,min(100,int(limit))))
            ).fetchall()
            return [dict(row) for row in rows]

    async def claim_creation_inspiration(self, inspiration_id: str) -> bool:
        """条件更新防止后台巡检和管理员命令同时消费同一份灵感。"""
        async with self._lock:
            cursor=self.conn.execute(
                "UPDATE creation_inspirations SET status='creating' WHERE id=? AND status='pending'",
                (inspiration_id,),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def recover_creation_claims(self, now: float) -> None:
        """Runner 异常退出后恢复未完成灵感，并封存残留的 running 记录。"""
        async with self._lock:
            with self._tx() as conn:
                running=conn.execute("SELECT document_id FROM creation_runs WHERE status='running'").fetchall()
                document_ids=[str(row[0]) for row in running if str(row[0] or "")]
                if document_ids:
                    marks=",".join("?" for _ in document_ids)
                    conn.execute(f"UPDATE bookshelf_documents SET status='failed',updated_at=? WHERE id IN ({marks})",
                                 (now,*document_ids))
                conn.execute(
                    "UPDATE creation_runs SET status='interrupted',completed_at=?,error_summary='Runner interrupted' WHERE status='running'",
                    (now,),
                )
                conn.execute("UPDATE creation_inspirations SET status='pending' WHERE status='creating' AND expires_at>?",(now,))
                conn.execute("UPDATE creation_inspirations SET status='expired' WHERE status='creating' AND expires_at<=?",(now,))

    async def mark_creation_inspiration(self, inspiration_id: str, status: str) -> None:
        async with self._lock:
            self.conn.execute("UPDATE creation_inspirations SET status=? WHERE id=?",(status[:24],inspiration_id))
            self.conn.commit()

    async def archived_work_count(self, start: float, end: float) -> int:
        async with self._lock:
            row=self.conn.execute(
                """SELECT COUNT(*) FROM bookshelf_documents WHERE doc_type='work' AND status='archived'
                AND archived_at>=? AND archived_at<?""",(start,end)
            ).fetchone()
            return int(row[0]) if row else 0

    async def create_bookshelf_document(self, item: dict[str, Any]) -> bool:
        async with self._lock:
            cursor=self.conn.execute(
                """INSERT OR IGNORE INTO bookshelf_documents
                (id,doc_type,work_type,title,privacy,status,current_revision,source_kind,source_ref,summary,
                 created_at,updated_at,archived_at)
                VALUES(?,?,?,?,?,?,0,?,?,?,?,?,0)""",
                (item["id"],item.get("doc_type","work")[:24],item.get("work_type","")[:40],item.get("title","未命名")[:160],
                 "public" if item.get("privacy")=="public" else "private",item.get("status","inspiration")[:24],
                 item.get("source_kind","")[:40],item.get("source_ref","")[:160],item.get("summary","")[:1000],
                 float(item["created_at"]),float(item["created_at"])),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def add_bookshelf_revision(self, document_id: str, stage: str, content: str, model_task: str,
                                     now: float, *, set_current: bool=True, status: str="") -> int:
        """追加不可变书柜修订，并可原子推进文档当前版本和流水线状态。"""
        async with self._lock:
            with self._tx() as conn:
                row=conn.execute(
                    "SELECT COALESCE(MAX(revision_number),0)+1 FROM bookshelf_revisions WHERE document_id=?",
                    (document_id,),
                ).fetchone(); revision=int(row[0] if row else 1)
                conn.execute(
                    """INSERT INTO bookshelf_revisions
                    (document_id,revision_number,stage,content,model_task,created_at) VALUES(?,?,?,?,?,?)""",
                    (document_id,revision,stage[:24],content[:20000],model_task[:80],now),
                )
                if set_current:
                    archived=now if status=="archived" else 0
                    conn.execute(
                        """UPDATE bookshelf_documents SET current_revision=?,status=COALESCE(NULLIF(?,''),status),
                        updated_at=?,archived_at=CASE WHEN ?>0 THEN ? ELSE archived_at END WHERE id=?""",
                        (revision,status[:24],now,archived,archived,document_id),
                    )
                return revision

    async def update_bookshelf_document(self, document_id: str, *, title: str="", summary: str="",
                                        privacy: str="", status: str="", now: float=0) -> None:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM bookshelf_documents WHERE id=?",(document_id,)).fetchone()
            if not row:return
            self.conn.execute(
                """UPDATE bookshelf_documents SET title=?,summary=?,privacy=?,status=?,updated_at=?,
                archived_at=CASE WHEN ?='archived' THEN ? ELSE archived_at END WHERE id=?""",
                (title[:160] or row["title"],summary[:1000] or row["summary"],
                 ("public" if privacy=="public" else "private") if privacy else row["privacy"],
                 status[:24] or row["status"],now or time.time(),status,now or time.time(),document_id),
            )
            self.conn.commit()

    async def start_creation_run(self, run_id: str, inspiration_id: str, document_id: str, now: float) -> None:
        async with self._lock:
            self.conn.execute(
                "INSERT INTO creation_runs(id,inspiration_id,document_id,status,started_at) VALUES(?,?,?,'running',?)",
                (run_id,inspiration_id,document_id,now),
            ); self.conn.commit()

    async def finish_creation_run(self, run_id: str, status: str, now: float, error: str="") -> None:
        async with self._lock:
            self.conn.execute(
                "UPDATE creation_runs SET status=?,completed_at=?,error_summary=? WHERE id=?",
                (status[:24],now,error[:500],run_id),
            ); self.conn.commit()

    async def list_bookshelf_documents(self, *, allow_private: bool, limit: int=20,
                                       doc_type: str="") -> list[dict[str, Any]]:
        async with self._lock:
            clauses=["d.status='archived'"]; params:list[Any]=[]
            if not allow_private:clauses.append("d.privacy='public'")
            if doc_type:clauses.append("d.doc_type=?"); params.append(doc_type)
            params.append(max(1,min(100,int(limit))))
            rows=self.conn.execute(
                f"""SELECT d.*,r.stage,r.content FROM bookshelf_documents d
                LEFT JOIN bookshelf_revisions r ON r.document_id=d.id AND r.revision_number=d.current_revision
                WHERE {' AND '.join(clauses)} ORDER BY d.updated_at DESC LIMIT ?""",params
            ).fetchall()
            return [dict(row) for row in rows]

    async def get_bookshelf_document(self, document_id: str, *, allow_private: bool) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM bookshelf_documents WHERE id=?",(document_id,)).fetchone()
            if not row or (row["privacy"]=="private" and not allow_private):return {}
            data=dict(row)
            revisions=self.conn.execute(
                """SELECT revision_number,stage,content,model_task,created_at FROM bookshelf_revisions
                WHERE document_id=? ORDER BY revision_number""",(document_id,)
            ).fetchall()
            data["revisions"]=[dict(item) for item in revisions]
            current=next((item for item in data["revisions"] if int(item["revision_number"])==int(data["current_revision"])),{})
            data["content"]=str(current.get("content") or "")
            return data

    async def save_reading_note(self, item: dict[str, Any]) -> bool:
        """外部阅读联动只接收有限文字摘要和生成批注，不接受二进制字段。"""
        async with self._lock:
            with self._tx() as conn:
                cursor=conn.execute(
                    """INSERT OR IGNORE INTO reading_notes
                    (id,source_api,external_id,title,summary,annotation,source_digest,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (item["id"],item["source_api"][:160],item["external_id"][:200],item["title"][:160],
                     item["summary"][:3000],item["annotation"][:4000],item["source_digest"][:128],float(item["created_at"])),
                )
                if not cursor.rowcount:return False
                document_id=f"reading:{item['id']}"; content=item["annotation"] or item["summary"]
                conn.execute(
                    """INSERT INTO bookshelf_documents
                    (id,doc_type,work_type,title,privacy,status,current_revision,source_kind,source_ref,summary,
                     created_at,updated_at,archived_at)
                    VALUES(?,'reading_note','',?,'private','archived',1,'external_reading',?,?,?,?,?)""",
                    (document_id,item["title"][:160],item["external_id"][:200],item["summary"][:1000],
                     float(item["created_at"]),float(item["created_at"]),float(item["created_at"])),
                )
                conn.execute(
                    """INSERT INTO bookshelf_revisions
                    (document_id,revision_number,stage,content,model_task,created_at)
                    VALUES(?,1,'annotation',?,'reading_annotation',?)""",
                    (document_id,content[:10000],float(item["created_at"])),
                )
                return True

    async def cleanup_creation(self, now: float) -> None:
        async with self._lock:
            self.conn.execute(
                "UPDATE creation_inspirations SET status='expired' WHERE status='pending' AND expires_at<=?",(now,)
            ); self.conn.commit()

    async def management_date_candidates(self, limit: int=100) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT id,user_id,event_name,date_text,suggested_date,confidence,created_at,status
                FROM date_candidates WHERE status='pending' ORDER BY created_at DESC LIMIT ?""",
                (max(1,min(500,int(limit))),),
            ).fetchall()
            return [dict(row) for row in rows]

    async def management_bookshelf(self, limit: int=100) -> list[dict[str, Any]]:
        """管理摘要不返回正文和修订内容，降低误暴露私密文本的风险。"""
        async with self._lock:
            rows=self.conn.execute(
                """SELECT id,doc_type,work_type,title,privacy,status,current_revision,source_kind,
                summary,created_at,updated_at,archived_at FROM bookshelf_documents
                ORDER BY updated_at DESC LIMIT ?""",(max(1,min(500,int(limit))),)
            ).fetchall()
            return [dict(row) for row in rows]

    async def management_proactive_candidates(self, limit: int=100) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT e.id,e.user_id,e.opportunity_id,e.status,e.created_at,e.expires_at,e.sent_at,
                o.topic,o.privacy,u.role FROM proactive_events e
                LEFT JOIN proactive_opportunities o ON o.id=e.opportunity_id
                LEFT JOIN users u ON u.user_id=e.user_id
                ORDER BY e.created_at DESC LIMIT ?""",(max(1,min(500,int(limit))),)
            ).fetchall()
            return [dict(row) for row in rows]

    async def management_creation_runs(self, limit: int=50) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT r.id,r.inspiration_id,r.document_id,r.status,r.started_at,r.completed_at,
                d.title,d.privacy,d.work_type FROM creation_runs r
                LEFT JOIN bookshelf_documents d ON d.id=r.document_id
                ORDER BY r.started_at DESC LIMIT ?""",(max(1,min(200,int(limit))),)
            ).fetchall()
            return [dict(row) for row in rows]

    async def management_overview_counts(self) -> dict[str, int]:
        """使用 COUNT 聚合，避免概览数字受明细分页上限影响。"""
        async with self._lock:
            row=self.conn.execute(
                """SELECT
                (SELECT COUNT(*) FROM users WHERE enabled=1) AS users,
                (SELECT COUNT(*) FROM users WHERE enabled=1 AND role='owner') AS owners,
                (SELECT COUNT(*) FROM date_candidates WHERE status='pending') AS pending_dates,
                (SELECT COUNT(*) FROM bookshelf_documents) AS bookshelf_documents,
                (SELECT COUNT(*) FROM bookshelf_documents WHERE privacy='private') AS private_documents,
                (SELECT COUNT(*) FROM proactive_events WHERE status='pending') AS pending_proactive"""
            ).fetchone()
            return {key:int(row[key] or 0) for key in row.keys()} if row else {}

    async def cleanup_runtime_records(self, now: float, usage_before: float) -> None:
        async with self._lock:
            with self._tx() as conn:
                conn.execute("DELETE FROM image_summaries WHERE expires_at<=?",(now,))
                conn.execute("DELETE FROM wake_candidates WHERE expires_at<=?",(now,))
                conn.execute("DELETE FROM reply_turns WHERE expires_at<=?",(now,))
                conn.execute("DELETE FROM message_turn_sources WHERE expires_at<=?",(now,))
                conn.execute(
                    "UPDATE recall_events SET summary='',media_types='[]',summary_expires_at=0 "
                    "WHERE summary_expires_at>0 AND summary_expires_at<=?",(now,)
                )
                conn.execute("DELETE FROM recall_events WHERE expires_at<=?",(now,))
                conn.execute("UPDATE proactive_events SET status='expired' WHERE status='pending' AND expires_at<=?",(now,))
                conn.execute("DELETE FROM llm_usage_events WHERE created_at<?",(usage_before,))
                conn.execute("DELETE FROM group_observations WHERE expires_at<=?",(now,))
                conn.execute("UPDATE relay_candidates SET status='expired' WHERE expires_at<=? AND status IN ('pending','sending','queued')",(now,))
                conn.execute("UPDATE creation_inspirations SET status='expired' WHERE status IN ('pending','creating') AND expires_at<=?",(now,))

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
        """保存私人日记，并同步维护书柜中的同一日记文档及其修订。"""
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO diary_entries
                    (day,created_at,title,content,mood_summary,privacy,source_digest)
                    VALUES(?,?,?,?,?,'private',?)""",
                    (day,created_at,title[:120],content[:4000],mood_summary[:300],source_digest[:128]),
                )
                document_id=f"diary:{day}"
                conn.execute(
                    """INSERT OR IGNORE INTO bookshelf_documents
                    (id,doc_type,work_type,title,privacy,status,current_revision,source_kind,source_ref,summary,
                     created_at,updated_at,archived_at)
                    VALUES(?,'diary','',?,'private','archived',1,'diary',?,?,?,?,?)""",
                    (document_id,title[:120],day,mood_summary[:500],created_at,created_at,created_at),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO bookshelf_revisions
                    (document_id,revision_number,stage,content,model_task,created_at)
                    VALUES(?,1,'diary',?,'diary',?)""",(document_id,content[:4000],created_at),
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
                                 recurrence: str, source: str, now: float,
                                 source_message_id: str="") -> int:
        """按用户、名称、日期和周期幂等保存明确的重要日期。"""
        clean_recurrence=recurrence if recurrence in {"none","annual"} else "none"
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO important_dates
                    (user_id,event_name,event_date,recurrence,source,created_at,updated_at,source_message_id)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (user_id,event_name[:120],event_date,clean_recurrence,source[:40],now,now,source_message_id[:240]),
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
                                 now: float, source_message_id: str="") -> int:
        """保存仍需当前 QQ 用户确认的模糊日期候选及其有限来源摘要。"""
        async with self._lock:
            existing=self.conn.execute(
                """SELECT id FROM date_candidates WHERE user_id=? AND event_name=? AND date_text=?
                AND status='pending' ORDER BY created_at DESC LIMIT 1""",
                (user_id,event_name[:120],date_text[:120]),
            ).fetchone()
            if existing:return int(existing[0])
            cursor=self.conn.execute(
                """INSERT INTO date_candidates
                (user_id,event_name,date_text,suggested_date,confidence,source_summary,created_at,status,source_message_id)
                VALUES(?,?,?,?,?,?,?,'pending',?)""",
                (user_id,event_name[:120],date_text[:120],suggested_date[:10],max(0,min(1,float(confidence))),
                 source_summary[:300],now,source_message_id[:240]),
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

    async def memory_runtime(self) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM memory_runtime WHERE id=1").fetchone()
            return dict(row) if row else {}

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
        """写入新闻；正文哈希变化时清除旧关联分数，强制重新进行自我关联。"""
        async with self._lock:
            existing=self.conn.execute("SELECT content_hash FROM news_items WHERE id=?",(item["id"],)).fetchone()
            self.conn.execute(
                """INSERT INTO news_items
                (id,source_id,title,url,summary,content,published_at,fetched_at,content_hash,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                source_id=excluded.source_id,title=excluded.title,url=excluded.url,
                summary=excluded.summary,content=excluded.content,published_at=excluded.published_at,
                fetched_at=excluded.fetched_at,
                relevance_score=CASE WHEN news_items.content_hash<>excluded.content_hash THEN 0 ELSE news_items.relevance_score END,
                relevance_reason=CASE WHEN news_items.content_hash<>excluded.content_hash THEN '' ELSE news_items.relevance_reason END,
                associated_at=CASE WHEN news_items.content_hash<>excluded.content_hash THEN 0 ELSE news_items.associated_at END,
                opportunity_id=CASE WHEN news_items.content_hash<>excluded.content_hash THEN '' ELSE news_items.opportunity_id END,
                content_hash=excluded.content_hash,expires_at=excluded.expires_at""",
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
        """保存来源退避状态，同时保留最近成功时间和可复用的 HTTP 缓存头。"""
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

    async def reconcile_search_keys(self,entries:list[tuple[str,str]],*,reset_existing:bool=False)->None:
        """配置热更新后只保留仍存在的 Key 指纹，原始 Key 从不入库。"""
        allowed={(str(provider),str(key)) for provider,key in entries if provider and key}
        async with self._lock:
            with self._tx() as conn:
                if reset_existing:
                    conn.execute("DELETE FROM search_key_runtime")
                    return
                rows=conn.execute("SELECT provider_id,key_fingerprint FROM search_key_runtime").fetchall()
                for row in rows:
                    if (str(row[0]),str(row[1])) not in allowed:
                        conn.execute("DELETE FROM search_key_runtime WHERE provider_id=? AND key_fingerprint=?",(row[0],row[1]))

    async def get_search_key_runtime(self,provider_id:str,key_fingerprint:str)->dict[str,Any]:
        async with self._lock:
            row=self.conn.execute(
                "SELECT * FROM search_key_runtime WHERE provider_id=? AND key_fingerprint=?",
                (provider_id,key_fingerprint),
            ).fetchone()
            return dict(row) if row else {"provider_id":provider_id,"key_fingerprint":key_fingerprint,
                "status":"healthy","cooldown_until":0,"failure_count":0,"last_error_class":"",
                "last_used_at":0,"last_success_at":0}

    async def save_search_key_runtime(self,provider_id:str,key_fingerprint:str,*,status:str,
                                      cooldown_until:float,failure_count:int,error_class:str,
                                      used_at:float,success_at:float=0)->None:
        async with self._lock:
            previous=self.conn.execute(
                "SELECT last_success_at FROM search_key_runtime WHERE provider_id=? AND key_fingerprint=?",
                (provider_id,key_fingerprint),
            ).fetchone()
            last_success=float(success_at or (previous[0] if previous else 0))
            self.conn.execute(
                """INSERT OR REPLACE INTO search_key_runtime
                (provider_id,key_fingerprint,status,cooldown_until,failure_count,last_error_class,last_used_at,last_success_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (provider_id[:80],key_fingerprint[:32],status[:24],float(cooldown_until),max(0,int(failure_count)),
                 error_class[:80],float(used_at),last_success),
            ); self.conn.commit()

    async def record_search_api_event(self,*,created_at:float,operation:str,provider_id:str,
                                      provider_type:str,key_fingerprint:str,success:bool,status_code:int,
                                      latency_ms:float,result_count:int,error_class:str)->None:
        async with self._lock:
            self.conn.execute(
                """INSERT INTO search_api_events
                (created_at,operation,provider_id,provider_type,key_fingerprint,success,status_code,
                 latency_ms,result_count,error_class) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (float(created_at),operation[:40],provider_id[:80],provider_type[:40],key_fingerprint[:32],
                 int(success),int(status_code),float(latency_ms),max(0,int(result_count)),error_class[:80]),
            ); self.conn.commit()

    async def search_api_summary(self,since:float)->list[dict[str,Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT provider_type,COUNT(*) calls,SUM(success) successes,
                SUM(result_count) results,AVG(latency_ms) average_latency_ms,
                MAX(created_at) last_at FROM search_api_events WHERE created_at>=?
                GROUP BY provider_type ORDER BY calls DESC""",(float(since),)
            ).fetchall()
            return [dict(row) for row in rows]

    async def search_success_count(self,operation:str,start:float,end:float)->int:
        """成功事件一一对应成功的逻辑搜索，失败的 Key 尝试不会占用每日额度。"""
        async with self._lock:
            row=self.conn.execute(
                """SELECT COUNT(*) FROM search_api_events
                WHERE operation=? AND success=1 AND created_at>=? AND created_at<?""",
                (operation[:40],float(start),float(end)),
            ).fetchone()
            return int(row[0]) if row else 0

    async def search_attempt_count(self,operation:str,start:float,end:float)->int:
        """同一次降级链共享 event_at，按逻辑搜索计数而不是按 Key 请求计数。"""
        async with self._lock:
            row=self.conn.execute(
                """SELECT COUNT(DISTINCT created_at) FROM search_api_events
                WHERE operation=? AND created_at>=? AND created_at<?""",
                (operation[:40],float(start),float(end)),
            ).fetchone()
            return int(row[0]) if row else 0

    async def search_provider_health(self)->list[dict[str,Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT provider_id,key_fingerprint,status,cooldown_until,failure_count,
                last_error_class,last_used_at,last_success_at FROM search_key_runtime
                ORDER BY provider_id,key_fingerprint"""
            ).fetchall()
            return [dict(row) for row in rows]

    async def cleanup_information(self, now: float) -> None:
        async with self._lock:
            with self._tx() as conn:
                conn.execute("DELETE FROM news_items WHERE expires_at<=?",(now,))
                conn.execute("DELETE FROM exploration_notes WHERE expires_at<=?",(now,))
                conn.execute("DELETE FROM search_api_events WHERE created_at<=?",(now-90*86400,))

    # 每个自然日只结算一次关系温度，离线补算时按结算日结束时刻判断冷却。
    async def update_relationships(self, day: str, day_start: float, day_end: float, now: float) -> None:
        """按日结算互动增量和长期不活跃衰减，并保证重复补算不会再次修改温度。"""
        del now
        async with self._lock:
            with self._tx() as conn:
                users=conn.execute("SELECT * FROM users WHERE enabled=1").fetchall()
                for user in users:
                    if str(user["last_relation_day"] or "")>=day: continue
                    msg_count=conn.execute(
                        "SELECT COUNT(*) FROM interaction_events WHERE user_id=? AND kind='message' AND created_at>=? AND created_at<?",
                        (user["user_id"],day_start,day_end),
                    ).fetchone()[0]
                    responded=conn.execute(
                        "SELECT 1 FROM interaction_events WHERE user_id=? AND kind='proactive_response' AND created_at>=? AND created_at<? LIMIT 1",
                        (user["user_id"],day_start,day_end),
                    ).fetchone()
                    delta=(0.5 if msg_count>0 else 0)+(0.5 if msg_count>=5 else 0)+(0.5 if responded else 0)
                    latest_before=conn.execute(
                        """SELECT MAX(created_at) FROM interaction_events
                        WHERE user_id=? AND kind='message' AND created_at<?""",
                        (user["user_id"],day_end),
                    ).fetchone()[0]
                    if msg_count==0 and latest_before and day_end-float(latest_before)>7*86400:
                        delta=-0.25
                    current_temperature=float(user["temperature"])
                    floor=min(20.0,current_temperature) if delta<0 else 0.0
                    temperature=max(floor,min(100.0,current_temperature+delta))
                    conn.execute("UPDATE users SET temperature=?,last_relation_day=? WHERE user_id=?",(temperature,day,user["user_id"]))

    async def get_weather(self) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM weather_cache WHERE id=1").fetchone()
            if not row: return {}
            data=dict(row)
            try:data["raw_json"]=json.loads(data.get("raw_json") or "{}")
            except (TypeError,json.JSONDecodeError):data["raw_json"]={}
            return data

    async def save_weather(self, data: dict[str, Any]) -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO weather_cache
                (id,fetched_at,location_name,latitude,longitude,temperature,weather_code,description,raw_json)
                VALUES(1,?,?,?,?,?,?,?,?)""",
                (data["fetched_at"],data["location_name"],data["latitude"],data["longitude"],
                 data.get("temperature"),data.get("weather_code"),data["description"],json.dumps(data.get("raw_json",{}),ensure_ascii=False)),
            ); self.conn.commit()

    async def clear_weather(self) -> None:
        async with self._lock:
            self.conn.execute("DELETE FROM weather_cache WHERE id=1")
            self.conn.commit()

