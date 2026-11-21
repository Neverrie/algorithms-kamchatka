"""Local persistent scenario/run repository; can be replaced by PostGIS later."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from .schema import ScenarioCreate, ScenarioRecord, RunRecord

class Store:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS scenarios (id TEXT PRIMARY KEY, json TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, json TEXT NOT NULL)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def create_scenario(self, request: ScenarioCreate, data_revision: str):
        record = ScenarioRecord(**request.model_dump(), scenario_id=str(uuid4()),
                                created_at=datetime.now(timezone.utc), data_revision=data_revision)
        with self._db() as db:
            db.execute("INSERT INTO scenarios VALUES (?,?)", (record.scenario_id,record.model_dump_json()))
        return record

    def get_scenario(self, scenario_id: str):
        with self._db() as db:
            row = db.execute("SELECT json FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
        return ScenarioRecord.model_validate_json(row[0]) if row else None

    def list_scenarios(self):
        with self._db() as db:
            rows = db.execute("SELECT json FROM scenarios ORDER BY rowid DESC LIMIT 50").fetchall()
        return [ScenarioRecord.model_validate_json(r[0]) for r in rows]

    def create_run(self, scenario_id: str, status: str, result=None, error=None):
        record = RunRecord(run_id=str(uuid4()), scenario_id=scenario_id,
                           created_at=datetime.now(timezone.utc), status=status,
                           result=result, error=error)
        with self._db() as db:
            db.execute("INSERT INTO runs VALUES (?,?)", (record.run_id,record.model_dump_json()))
        return record

    def get_run(self, run_id: str):
        with self._db() as db:
            row = db.execute("SELECT json FROM runs WHERE id=?", (run_id,)).fetchone()
        return RunRecord.model_validate_json(row[0]) if row else None

    def list_runs(self, scenario_id: str):
        with self._db() as db:
            rows = db.execute(
                "SELECT json FROM runs WHERE json_extract(json, '$.scenario_id') = ? ORDER BY rowid DESC LIMIT 25",
                (scenario_id,)).fetchall()
        return [RunRecord.model_validate_json(row[0]) for row in rows]
