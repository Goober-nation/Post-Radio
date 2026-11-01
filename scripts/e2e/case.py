"""One end-to-end case, run by scripts/e2e/run.py inside the venv under test (never imported by it).

    python case.py <spec.json> <out.json>

spec: {"home", "kind": "flow"|"radio"|"profile", "args": {...}, "yandex_url", "yandex_export" | null,
       "repeat": bool, "dry_first": bool}
The ListenBrainz token comes from $LISTENBRAINZ_TOKEN and is passed per call (Listener.lb_token); it is never
written to the output. With `yandex_export`, Yandex's playlist call is answered from that file (the cloud is
blocked by Yandex); every other part of the Yandex path (likes, dates, album genres) runs as usual.
"""
import dataclasses
import json
import logging
import os
import sys
import time
import traceback

spec = json.load(open(sys.argv[1]))
out_path = sys.argv[2]
events: list[dict] = []
t0 = time.monotonic()


def write(result: dict) -> None:
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, out_path)


class Capture(logging.Handler):
    def emit(self, record):
        events.append({"t": round(time.monotonic() - t0, 2), "log": record.getMessage()[:300]})


logging.getLogger("postradio").addHandler(Capture())
logging.getLogger("postradio").setLevel(logging.INFO)

import postradio  # noqa: E402
from postradio.api import Listener, Recommender  # noqa: E402
from postradio.brand import VERSION  # noqa: E402
from postradio.providers import yandex as ymod  # noqa: E402

if spec.get("yandex_export"):
    raw = json.load(open(spec["yandex_export"]))
    replay = ymod.Playlist(raw["title"], raw["total"], [
        ymod.LikedTrack(tuple(tuple(a) for a in t["artists"]), t["title"], t.get("added_at"), t.get("genre"))
        for t in raw["tracks"]])

    def _playlist(self, url, limit=None, log=lambda _m: None):
        return dataclasses.replace(replay, tracks=replay.tracks[:limit] if limit else list(replay.tracks))

    ymod.YandexClient.playlist = _playlist


def progress(p):
    events.append({"t": round(time.monotonic() - t0, 2), "stage": p.stage, "done": p.done, "total": p.total})


result: dict = {"spec": {k: v for k, v in spec.items() if k != "home"}, "version": VERSION,
                "postradio_file": postradio.__file__, "runs": []}
token = os.environ.get("LISTENBRAINZ_TOKEN") or None
try:
    with Recommender(home=spec["home"]) as rec:
        result["store"] = rec.store is not None
        listener = Listener(yandex_playlist=spec["yandex_url"], lb_token=token) if spec.get("yandex_url") else None
        call = {"flow": rec.flow, "radio": rec.generate, "profile": None}[spec["kind"]]
        plan = []
        if spec.get("dry_first"):
            plan.append(("dry", {**spec["args"], "dry": True}))
        plan.append(("main", dict(spec["args"])))
        for label, args in plan:
            calls0, fails0 = rec.http.calls, rec.http.failures
            start = time.monotonic()
            events.append({"t": round(start - t0, 2), "run": label})
            try:
                if spec["kind"] == "profile":
                    res = rec.profile(listener, progress=progress)
                else:
                    res = call(**args, listener=listener, progress=progress)
                run = {"label": label, "ok": True, "seconds": round(time.monotonic() - start, 2),
                       "result": res.to_dict()}
            except Exception as e:  # noqa: BLE001 - recorded, graded by run.py
                run = {"label": label, "ok": False, "seconds": round(time.monotonic() - start, 2),
                       "error": {"type": type(e).__name__, "message": str(e)[:500]},
                       "timed_out": bool(getattr(e, "timed_out", False))}
            run["http_calls"], run["http_failures"] = rec.http.calls - calls0, rec.http.failures - fails0
            result["runs"].append(run)
            write({**result, "events": events, "partial": True})
            if label == "main" and spec.get("repeat") and run["ok"]:
                seed = run["result"].get("rng_seed")
                start = time.monotonic()
                again = call(**{**args, "rng_seed": seed}, listener=listener)
                result["runs"].append({"label": "repeat", "ok": True, "seconds": round(time.monotonic() - start, 2),
                                       "result": again.to_dict()})
except Exception as e:  # noqa: BLE001
    result["crash"] = {"type": type(e).__name__, "message": str(e)[:500], "trace": traceback.format_exc()[-2000:]}
result["seconds"] = round(time.monotonic() - t0, 2)
write({**result, "events": events, "partial": False})
