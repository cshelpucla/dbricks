"""Control tab: pause or resume the pipeline resources, one at a time or all together.

Pausing a continuous job sets its trigger to PAUSED and cancels the running run (the same as
bluesky/stop.sh); resuming unpauses it and the trigger starts a fresh run, which resumes from the job's
own cursor. The silver pipeline is stopped and started. The app's service principal needs CAN_MANAGE
on the jobs (changing the pause setting is a settings edit) and CAN_RUN on the pipeline.
"""
from .registry import BY_KEY, RESOURCES

ACTIONS = ("pause", "resume")
# Pause downstream first so nothing waits on missing input; resume upstream first.
PAUSE_ORDER = ("map", "enrich", "ingest", "silver")
RESUME_ORDER = ("ingest", "silver", "enrich", "map")
IMPACT = {
    "ingest": "New Bluesky events stop arriving. Jetstream keeps 36 hours of history, so resuming within 36 hours "
              "back-fills the gap; a longer pause loses data permanently.",
    "silver": "The posts, likes, reposts and follows tables stop updating. On resume the pipeline processes "
              "everything that arrived in the meantime.",
    "enrich": "Bot filtering, geo, embeddings and topics stop. On resume the job catches up from where it stopped, "
              "reading the raw table.",
    "map": "The topic map stops refreshing; the last map stays visible. On resume it refreshes at the next "
           "scheduled time (every 2 hours).",
}


class LiveControl:
    mode = "live"

    def __init__(self, pipelines):
        self.pipelines = pipelines        # LivePipelines, for status

    def resources(self) -> list[dict]:
        out = []
        for r in RESOURCES:
            s = self.pipelines.status(r)
            out.append({"key": r.key, "name": r.name, "kind": r.kind, "layer": r.layer, "status": s["status"],
                        "url": s["url"], "impact": IMPACT[r.key]})
        return out

    def act(self, key: str, action: str) -> dict:
        from databricks.sdk.service.jobs import Continuous, CronSchedule, JobSettings, PauseStatus

        from . import sqlexec
        r = BY_KEY[key]
        w = sqlexec.client()
        if r.kind == "job":
            jobs = list(w.jobs.list(name=r.name))
            if not jobs:
                return {"key": key, "ok": False, "message": "not deployed"}
            job_id = jobs[0].job_id
            pause = PauseStatus.PAUSED if action == "pause" else PauseStatus.UNPAUSED
            sched = w.jobs.get(job_id).settings.schedule
            if sched and not jobs[0].settings.continuous:
                # scheduled job: pause the schedule and let a run in progress finish
                w.jobs.update(job_id=job_id, new_settings=JobSettings(schedule=CronSchedule(
                    quartz_cron_expression=sched.quartz_cron_expression, timezone_id=sched.timezone_id, pause_status=pause)))
            else:
                w.jobs.update(job_id=job_id, new_settings=JobSettings(continuous=Continuous(pause_status=pause)))
                if action == "pause":
                    w.jobs.cancel_all_runs(job_id=job_id)
        else:
            found = list(w.pipelines.list_pipelines(filter=f"name LIKE '{r.name}'"))
            if not found:
                return {"key": key, "ok": False, "message": "not deployed"}
            pid = found[0].pipeline_id
            if action == "pause":
                w.pipelines.stop(pid)          # returns without waiting for the pipeline to reach IDLE
            else:
                w.pipelines.start_update(pid)
        return {"key": key, "ok": True, "message": f"{action} requested"}


class DemoControl:
    """In-memory stand-in so the tab can be tried without touching real resources."""
    mode = "demo"

    def __init__(self):
        self.state = {"ingest": "running", "silver": "running", "enrich": "not deployed", "map": "scheduled"}

    def resources(self):
        return [{"key": r.key, "name": r.name, "kind": r.kind, "layer": r.layer, "status": self.state[r.key],
                 "url": None, "impact": IMPACT[r.key]} for r in RESOURCES]

    def act(self, key, action):
        if self.state[key] == "not deployed":
            return {"key": key, "ok": False, "message": "not deployed"}
        self.state[key] = "paused" if action == "pause" else ("scheduled" if key == "map" else "running")
        return {"key": key, "ok": True, "message": f"{action} done (demo)"}
