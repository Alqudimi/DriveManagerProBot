from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

log = logging.getLogger(__name__)


@dataclass
class Job:
    job_id: str
    owner_id: int
    kind: str
    status: str = "queued"
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str = ""
    progress: int = 0
    phase: str = ""
    error: str = ""


class JobManager:
    def __init__(self, journal: Path, limit: int = 2):
        self.journal = journal
        self.semaphore = asyncio.Semaphore(limit)
        self.jobs: dict[str, Job] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._restore()

    def _restore(self) -> None:
        if not self.journal.exists():
            return
        try:
            records = json.loads(self.journal.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                raise ValueError("سجل الوظائف ليس قائمة")
            recovered = False
            for record in records:
                job = Job(**record)
                if job.status in {"queued", "running"}:
                    job.status = "needs_recovery"
                    job.error = "انتهت جلسة Colab قبل تأكيد اكتمال العملية؛ تحقق من الوجهة قبل إعادة المحاولة."
                    recovered = True
                self.jobs[job.job_id] = job
            if recovered:
                self._persist()
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            backup = self.journal.with_suffix(self.journal.suffix + ".corrupt")
            try:
                self.journal.replace(backup)
            except OSError as exc:
                log.warning("Could not preserve invalid job journal: %s", type(exc).__name__)
            self.jobs = {}

    def create(self, owner_id: int, kind: str) -> Job:
        job = Job(str(uuid4())[:8], owner_id, kind)
        self.jobs[job.job_id] = job
        self._persist()
        return job

    async def run(self, job: Job, coroutine) -> object:
        try:
            async with self.semaphore:
                job.status = "running"
                self._persist()
                result = await coroutine
                job.status = "completed"
                return result
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as exc:
            job.status, job.error = "failed", str(exc)[:500]
            raise
        finally:
            job.finished_at = datetime.now(timezone.utc).isoformat()
            self._persist()

    def cancel(self, job_id: str, owner_id: int) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.owner_id != owner_id:
            return False
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    def owned(self, owner_id: int) -> list[Job]:
        return [job for job in self.jobs.values() if job.owner_id == owner_id]

    def _persist(self) -> None:
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        temp = self.journal.with_suffix(".tmp")
        temp.write_text(json.dumps([asdict(j) for j in self.jobs.values()], ensure_ascii=False), encoding="utf-8")
        temp.replace(self.journal)
