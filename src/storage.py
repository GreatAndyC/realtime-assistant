"""SQLite meeting records and append-only PCM audio."""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

from .config import config
from .models import AgentState, Language, Meeting, MinutesData, PhaseSummary, Utterance


_SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


class Storage:
    def __init__(self, db_path: Path | None = None, data_dir: Path | None = None):
        self.db_path = Path(db_path or config.db_path)
        self.data_dir = Path(data_dir or config.data_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS meetings (
                meeting_id TEXT PRIMARY KEY, language TEXT NOT NULL, speaker TEXT NOT NULL,
                started_at_ms INTEGER NOT NULL, ended_at_ms INTEGER,
                state TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT -1,
                next_sample_offset INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS utterances (
                utterance_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id),
                speaker TEXT NOT NULL, language TEXT NOT NULL, text TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL, seq INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS utterances_meeting_time ON utterances(meeting_id, timestamp_ms);
            CREATE TABLE IF NOT EXISTS phase_summaries (
                summary_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id),
                created_at_ms INTEGER NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS minutes (
                meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), phase TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(meeting_id, phase)
            );
        """)
        self._conn.commit()

    @staticmethod
    def validate_id(meeting_id: str) -> str:
        if not _SAFE_ID.fullmatch(meeting_id):
            raise ValueError("meeting_id must contain only letters, digits, _ or -")
        return meeting_id

    def create_or_resume(self, meeting_id: str, language: Language, speaker: str) -> Meeting:
        self.validate_id(meeting_id)
        with self._lock:
            row = self._conn.execute("SELECT * FROM meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
            if row is None:
                started = int(time.time() * 1000)
                self._conn.execute(
                    "INSERT INTO meetings(meeting_id, language, speaker, started_at_ms, state) VALUES(?,?,?,?,?)",
                    (meeting_id, language.value, speaker, started, AgentState.LISTENING.value),
                )
                self._conn.commit()
                row = self._conn.execute("SELECT * FROM meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
            elif row["state"] == AgentState.ENDED.value:
                raise ValueError("meeting already ended")
            return self._meeting(row)

    def get_meeting(self, meeting_id: str) -> Meeting | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
            return self._meeting(row) if row else None

    @staticmethod
    def _meeting(row: sqlite3.Row) -> Meeting:
        return Meeting(**{k: row[k] for k in (
            "meeting_id", "language", "speaker", "started_at_ms", "ended_at_ms", "state"
        )})

    def audio_position(self, meeting_id: str) -> tuple[int, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seq, next_sample_offset FROM meetings WHERE meeting_id=?", (meeting_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown meeting")
            return row["last_seq"], row["next_sample_offset"]

    def append_audio(self, meeting_id: str, seq: int, sample_offset: int, pcm: bytes) -> bool:
        """Returns False for a duplicate frame; rejects gaps and conflicting offsets."""
        self.validate_id(meeting_id)
        if not pcm or len(pcm) % 2 or seq < 0 or sample_offset < 0:
            raise ValueError("invalid audio frame")
        with self._lock:
            last_seq, expected_offset = self.audio_position(meeting_id)
            if seq <= last_seq:
                return False
            if seq != last_seq + 1 or sample_offset != expected_offset:
                raise ValueError(f"audio gap: expected seq={last_seq + 1}, offset={expected_offset}")
            audio_path = self.data_dir / "meetings" / meeting_id / "audio.pcm"
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            with audio_path.open("r+b" if audio_path.exists() else "w+b") as audio_file:
                audio_file.seek(0, 2)
                if audio_file.tell() < sample_offset * 2:
                    raise ValueError("audio file is shorter than the recorded offset")
                audio_file.seek(sample_offset * 2)
                audio_file.write(pcm)
                audio_file.flush()
            self._conn.execute(
                "UPDATE meetings SET last_seq=?, next_sample_offset=? WHERE meeting_id=?",
                (seq, sample_offset + len(pcm) // 2, meeting_id),
            )
            self._conn.commit()
            return True

    def save_utterance(self, utterance: Utterance) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO utterances VALUES(?,?,?,?,?,?,?)",
                (utterance.utterance_id, utterance.meeting_id, utterance.speaker,
                 utterance.language.value, utterance.text, utterance.timestamp_ms, utterance.seq),
            )
            self._conn.commit()

    def list_utterances(self, meeting_id: str, limit: int | None = None) -> list[Utterance]:
        with self._lock:
            if limit is None:
                rows = self._conn.execute(
                    "SELECT * FROM utterances WHERE meeting_id=? ORDER BY timestamp_ms, rowid", (meeting_id,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM utterances WHERE meeting_id=? "
                    "ORDER BY timestamp_ms DESC, rowid DESC LIMIT ?",
                    (meeting_id, limit),
                ).fetchall()
                rows = list(reversed(rows))
            return [Utterance(**dict(row)) for row in rows]

    def count_utterances(self, meeting_id: str) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM utterances WHERE meeting_id=?", (meeting_id,)
            ).fetchone()[0]

    def save_phase_summary(self, summary: PhaseSummary) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO phase_summaries VALUES(?,?,?,?)",
                (summary.summary_id, summary.meeting_id, summary.created_at_ms,
                 summary.model_dump_json()),
            )
            self._conn.commit()

    def list_phase_summaries(self, meeting_id: str) -> list[PhaseSummary]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM phase_summaries WHERE meeting_id=? ORDER BY created_at_ms", (meeting_id,)
            ).fetchall()
            return [PhaseSummary.model_validate_json(row[0]) for row in rows]

    def save_minutes(self, meeting_id: str, minutes: MinutesData) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO minutes VALUES(?,?,?,?)",
                (meeting_id, minutes.phase, int(time.time() * 1000), minutes.model_dump_json()),
            )
            self._conn.commit()

    def get_minutes(self, meeting_id: str, phase: str = "final") -> MinutesData | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM minutes WHERE meeting_id=? AND phase=?", (meeting_id, phase)
            ).fetchone()
            return MinutesData.model_validate_json(row[0]) if row else None

    def set_state(self, meeting_id: str, state: AgentState) -> None:
        with self._lock:
            ended_at = int(time.time() * 1000) if state == AgentState.ENDED else None
            self._conn.execute(
                "UPDATE meetings SET state=?, ended_at_ms=COALESCE(?, ended_at_ms) WHERE meeting_id=?",
                (state.value, ended_at, meeting_id),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
