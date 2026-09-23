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
            CREATE TABLE IF NOT EXISTS cloud_asr_checkpoints (
                meeting_id TEXT PRIMARY KEY REFERENCES meetings(meeting_id),
                sample_offset INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS audio_frames (
                meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id),
                seq INTEGER NOT NULL, start_sample INTEGER NOT NULL,
                end_sample INTEGER NOT NULL, received_at_ms INTEGER NOT NULL,
                PRIMARY KEY(meeting_id, seq)
            );
            CREATE INDEX IF NOT EXISTS audio_frames_meeting_end
                ON audio_frames(meeting_id, end_sample);
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
            audio_path = self.data_dir / "meetings" / meeting_id / "audio.pcm"
            if seq <= last_seq:
                if sample_offset + len(pcm) // 2 > expected_offset:
                    raise ValueError("duplicate audio frame exceeds stored audio")
                try:
                    with audio_path.open("rb") as audio_file:
                        audio_file.seek(sample_offset * 2)
                        if audio_file.read(len(pcm)) != pcm:
                            raise ValueError("duplicate audio frame conflicts with stored audio")
                except FileNotFoundError as exc:
                    raise ValueError("stored audio is missing") from exc
                return False
            if seq != last_seq + 1 or sample_offset != expected_offset:
                raise ValueError(f"audio gap: expected seq={last_seq + 1}, offset={expected_offset}")
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
            self._conn.execute(
                "INSERT INTO audio_frames VALUES(?,?,?,?,?)",
                (meeting_id, seq, sample_offset, sample_offset + len(pcm) // 2,
                 int(time.time() * 1000)),
            )
            self._conn.commit()
            return True

    def audio_frame_at(self, meeting_id: str, end_sample: int) -> tuple[int, int] | None:
        """Return the sequence and capture time for a recognized sample offset."""
        with self._lock:
            row = self._conn.execute(
                "SELECT seq, received_at_ms FROM audio_frames "
                "WHERE meeting_id=? AND start_sample<? AND end_sample>=? "
                "ORDER BY end_sample LIMIT 1",
                (meeting_id, end_sample, end_sample),
            ).fetchone()
            return (row[0], row[1]) if row else None

    def save_utterance(self, utterance: Utterance) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO utterances VALUES(?,?,?,?,?,?,?)",
                (utterance.utterance_id, utterance.meeting_id, utterance.speaker,
                 utterance.language.value, utterance.text, utterance.timestamp_ms, utterance.seq),
            )
            self._conn.commit()

    def cloud_asr_checkpoint(self, meeting_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT sample_offset FROM cloud_asr_checkpoints WHERE meeting_id=?", (meeting_id,)
            ).fetchone()
            return row[0] if row else 0

    def read_audio_since(self, meeting_id: str, sample_offset: int) -> bytes:
        """Read persisted PCM that has not yet been finalized by cloud ASR."""
        self.validate_id(meeting_id)
        with self._lock:
            _, end_offset = self.audio_position(meeting_id)
            if not 0 <= sample_offset <= end_offset:
                raise ValueError("invalid ASR checkpoint")
            if sample_offset == end_offset:
                return b""
            audio_path = self.data_dir / "meetings" / meeting_id / "audio.pcm"
            with audio_path.open("rb") as audio_file:
                audio_file.seek(sample_offset * 2)
                pcm = audio_file.read((end_offset - sample_offset) * 2)
            if len(pcm) != (end_offset - sample_offset) * 2:
                raise ValueError("stored audio is shorter than the ASR checkpoint range")
            return pcm

    def save_cloud_asr_utterance(self, utterance: Utterance, end_sample: int | None) -> bool:
        """Save a final subtitle and its replay checkpoint in one transaction."""
        with self._lock:
            if end_sample is not None:
                _, stored_end = self.audio_position(utterance.meeting_id)
                if not 0 <= end_sample <= stored_end:
                    raise ValueError("cloud ASR checkpoint exceeds stored audio")
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO utterances VALUES(?,?,?,?,?,?,?)",
                (utterance.utterance_id, utterance.meeting_id, utterance.speaker,
                 utterance.language.value, utterance.text, utterance.timestamp_ms, utterance.seq),
            )
            if end_sample is not None:
                self._conn.execute(
                    "INSERT INTO cloud_asr_checkpoints(meeting_id, sample_offset) VALUES(?,?) "
                    "ON CONFLICT(meeting_id) DO UPDATE SET sample_offset="
                    "MAX(sample_offset, excluded.sample_offset)",
                    (utterance.meeting_id, end_sample),
                )
            self._conn.commit()
            return cursor.rowcount == 1

    def set_cloud_asr_checkpoint(self, meeting_id: str, sample_offset: int) -> None:
        with self._lock:
            _, stored_end = self.audio_position(meeting_id)
            if not 0 <= sample_offset <= stored_end:
                raise ValueError("cloud ASR checkpoint exceeds stored audio")
            self._conn.execute(
                "INSERT INTO cloud_asr_checkpoints(meeting_id, sample_offset) VALUES(?,?) "
                "ON CONFLICT(meeting_id) DO UPDATE SET sample_offset="
                "MAX(sample_offset, excluded.sample_offset)",
                (meeting_id, sample_offset),
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
