"""
Invia a Garmin Connect gli allenamenti STRUTTURATI approvati nell'app (come il builder di Garmin:
riscaldamento, ripetute, recuperi, defaticamento, con durata/distanza e target di ritmo, FC, potenza,
cadenza o zona). Una volta in calendario Garmin passano da soli su orologio e Edge.

L'app scrive le sedute in `trainingPlan` dentro DietaElia-backup.json; qui le si legge (file locale o
OneDrive via Graph) e si sincronizza Garmin. Il backup dell'app NON viene mai modificato: l'esito
(id workout, id pianificazione, hash) sta in <dest>/workouts_sync.json, che l'app puo' leggere.

    python scripts/garmin_workouts.py --dest "C:\\Users\\eliag\\OneDrive\\GarminData" \\
        --backup "C:\\Users\\eliag\\OneDrive\\DietaElia-backup.json"
    python scripts/garmin_workouts.py --dest <cartella> --backup-graph        (GitHub Action)

Formato `struttura` di una seduta (lo produce il coach):
    {"sport": "running|cycling|swimming",
     "steps": [ {"kind": "warmup|interval|recovery|rest|cooldown|repeat",
                 "end": "time|distance|lap", "value": secondi|metri,
                 "target": "none|pace|heart_rate|heart_rate_zone|power|power_zone|cadence",
                 "low": n, "high": n, "note": "testo",
                 "repeat": iterazioni, "steps": [ ...passi del gruppo... ]} ]}
    pace: low/high in secondi per km (nuoto: secondi per 100 m); *_zone: low = numero zona.
"""
import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from garmin_archive import LocalStore, log, login

MARKER = "[coach:{id}]"
NAME_PREFIX = "Coach - "

SPORTS = {
    "running": (1, "running"),
    "cycling": (2, "cycling"),
    "swimming": (4, "swimming"),
}
STEP_TYPES = {
    "warmup": (1, "warmup"), "cooldown": (2, "cooldown"), "interval": (3, "interval"),
    "recovery": (4, "recovery"), "rest": (5, "rest"), "repeat": (6, "repeat"),
    "main": (3, "interval"),
}
END_CONDITIONS = {
    "lap": (1, "lap.button", True),
    "time": (2, "time", True),
    "distance": (3, "distance", True),
}
TARGETS = {
    "none": (1, "no.target"), "power": (2, "power.zone"), "power_zone": (2, "power.zone"),
    "cadence": (3, "cadence"), "heart_rate": (4, "heart.rate.zone"), "heart_rate_zone": (4, "heart.rate.zone"),
    "pace": (6, "pace.zone"),
}


class WorkoutError(ValueError):
    pass


def _num(v, what):
    try:
        return float(v)
    except (TypeError, ValueError):
        raise WorkoutError(f"{what}: numero non valido ({v!r})")


def _target(step, sport):
    kind = step.get("target") or "none"
    if kind not in TARGETS:
        raise WorkoutError(f"target sconosciuto: {kind}")
    tid, tkey = TARGETS[kind]
    out = {"targetType": {"workoutTargetTypeId": tid, "workoutTargetTypeKey": tkey, "displayOrder": tid},
           "targetValueOne": None, "targetValueTwo": None, "zoneNumber": None}
    if kind == "none":
        return out
    low, high = step.get("low"), step.get("high")
    if kind.endswith("_zone"):
        z = int(_num(low, "zona"))
        if not 1 <= z <= 7:
            raise WorkoutError(f"zona fuori range: {z}")
        out["zoneNumber"] = z
        return out
    lo, hi = _num(low, "target low"), _num(high if high is not None else low, "target high")
    if lo > hi:
        lo, hi = hi, lo
    if kind == "pace":
        per = 100.0 if sport == "swimming" else 1000.0
        if lo <= 0:
            raise WorkoutError("ritmo non valido")
        out["targetValueOne"] = round(per / lo, 7)   # ritmo piu' veloce = m/s piu' alto, va per primo
        out["targetValueTwo"] = round(per / hi, 7)
    else:
        out["targetValueOne"], out["targetValueTwo"] = lo, hi
    return out


class Builder:
    def __init__(self, sport):
        self.sport = sport
        self.order = 0
        self.groups = 0
        self.seconds = 0.0

    def steps(self, items, child_of=None, depth=0):
        if not isinstance(items, list) or not items:
            raise WorkoutError("lista passi vuota")
        if depth > 2:
            raise WorkoutError("troppi livelli di ripetizioni")
        out = []
        for st in items:
            kind = st.get("kind")
            if kind not in STEP_TYPES:
                raise WorkoutError(f"tipo di passo sconosciuto: {kind}")
            self.order += 1
            if kind == "repeat":
                n = int(_num(st.get("repeat"), "ripetizioni"))
                if not 1 <= n <= 99:
                    raise WorkoutError(f"ripetizioni fuori range: {n}")
                self.groups += 1
                gid = self.groups
                grp = {"type": "RepeatGroupDTO", "stepOrder": self.order, "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
                       "childStepId": gid, "numberOfIterations": n, "endConditionValue": float(n),
                       "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
                       "smartRepeat": False, "workoutSteps": []}
                before = self.seconds
                grp["workoutSteps"] = self.steps(st.get("steps"), gid, depth + 1)
                self.seconds = before + (self.seconds - before) * n
                out.append(grp)
                continue
            tid, tkey = STEP_TYPES[kind]
            end = st.get("end") or "lap"
            if end not in END_CONDITIONS:
                raise WorkoutError(f"condizione di fine sconosciuta: {end}")
            cid, ckey, disp = END_CONDITIONS[end]
            value = 0.0 if end == "lap" else _num(st.get("value"), "durata/distanza")
            if end != "lap" and value <= 0:
                raise WorkoutError("durata/distanza deve essere > 0")
            if end == "time":
                self.seconds += value
            elif end == "distance":
                self.seconds += value / (3.0 if self.sport == "running" else 7.0 if self.sport == "cycling" else 0.8)
            ex = {"type": "ExecutableStepDTO", "stepOrder": self.order, "stepType": {"stepTypeId": tid, "stepTypeKey": tkey, "displayOrder": tid},
                  "childStepId": child_of, "description": (st.get("note") or "")[:512] or None,
                  "endCondition": {"conditionTypeId": cid, "conditionTypeKey": ckey, "displayOrder": cid, "displayable": disp},
                  "endConditionValue": value,
                  "strokeType": {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0},
                  "equipmentType": {"equipmentTypeId": 0, "equipmentTypeKey": None, "displayOrder": 0}}
            if end == "distance":
                ex["preferredEndConditionUnit"] = {"unitId": 2, "unitKey": "kilometer", "factor": 100000.0}
            ex.update(_target(st, self.sport))
            out.append(ex)
        return out


def build_workout(item):
    st = item.get("struttura") or {}
    sport = st.get("sport")
    if sport not in SPORTS:
        raise WorkoutError(f"sport non supportato: {sport}")
    sid, skey = SPORTS[sport]
    b = Builder(sport)
    steps = b.steps(st.get("steps"))
    sport_type = {"sportTypeId": sid, "sportTypeKey": skey, "displayOrder": sid}
    label = {"running": "Corsa", "cycling": "Bici", "swimming": "Nuoto"}[sport]
    name = (NAME_PREFIX + (item.get("titolo") or label + " " + item["data"]))[:80]
    desc = ((item.get("descrizione") or "")[:400] + " " + MARKER.format(id=item["id"])).strip()
    dur = int(round(b.seconds)) or None
    return {
        "workoutName": name, "description": desc, "sportType": sport_type,
        "estimatedDurationInSecs": dur,
        "workoutSegments": [{"segmentOrder": 1, "sportType": sport_type, "workoutSteps": steps}],
    }


def digest(item):
    payload = json.dumps({"s": item.get("struttura"), "t": item.get("titolo"), "d": item.get("descrizione")}, sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def load_plan_local(path):
    snap = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = (snap.get("data") or {}).get("trainingPlan")
    return json.loads(raw) if raw else []


def load_plan_graph():
    import sync_garmin as sg  # riusa l'autenticazione Graph gia' presente
    token = sg.graph_acquire_token()
    if not token:
        raise SystemExit("Token Microsoft non disponibile (vedi README: setup OneDrive privato)")
    snap = sg.graph_get_json(token, "/me/drive/root:/DietaElia-backup.json")
    raw = (snap.get("data") or {}).get("trainingPlan")
    return json.loads(raw) if raw else []


def sync(client, store, plan, today, dry_run=False):
    state = store.read_json("workouts_sync.json", {}) or {}
    result = {"created": 0, "updated": 0, "removed": 0, "errors": []}
    wanted = {}
    for it in plan:
        if it.get("struttura") and not it.get("fatto") and it.get("data") and it["data"] >= today.isoformat():
            wanted[it["id"]] = it

    for pid, it in wanted.items():
        h = digest(it)
        cur = state.get(pid)
        try:
            payload = build_workout(it)
        except WorkoutError as e:
            result["errors"].append(f"{pid}: {e}")
            state[pid] = {**(cur or {}), "error": str(e), "hash": None}
            continue
        if cur and cur.get("hash") == h and cur.get("date") == it["data"] and cur.get("scheduledId"):
            continue
        if dry_run:
            log(f"[dry-run] {'aggiorno' if cur else 'creo'} {payload['workoutName']} il {it['data']}")
            continue
        try:
            if cur and cur.get("workoutId"):
                client.update_workout(cur["workoutId"], payload)
                wid = cur["workoutId"]
                if cur.get("date") != it["data"] and cur.get("scheduledId"):
                    client.unschedule_workout(cur["scheduledId"])
                    cur["scheduledId"] = None
                result["updated"] += 1
            else:
                r = client.upload_workout(payload)
                wid = r.get("workoutId")
                if not wid:
                    raise RuntimeError("Garmin non ha restituito workoutId")
                result["created"] += 1
            sched = cur.get("scheduledId") if cur else None
            if not sched:
                r2 = client.schedule_workout(wid, it["data"])
                sched = r2.get("workoutScheduleId") or r2.get("scheduledWorkoutId") or r2.get("id")
            state[pid] = {"workoutId": wid, "scheduledId": sched, "hash": h, "date": it["data"], "name": payload["workoutName"],
                          "syncedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            log(f"OK {payload['workoutName']} -> workout {wid}, {it['data']}")
        except Exception as e:  # noqa: BLE001 - un errore non deve bloccare le altre sedute
            result["errors"].append(f"{pid}: {type(e).__name__}: {str(e)[:160]}")

    # sedute tolte dall'app (o senza piu' struttura): rimuovo SOLO quelle create da qui e ancora future
    for pid in list(state):
        if pid in wanted:
            continue
        cur = state[pid]
        item = next((x for x in plan if x.get("id") == pid), None)
        if item and (item.get("fatto") or (item.get("data") or "9999") < today.isoformat()):
            continue  # gia' fatta o passata: la lascio in Garmin
        if dry_run:
            log(f"[dry-run] rimuovo {cur.get('name')}")
            continue
        try:
            if cur.get("scheduledId"):
                client.unschedule_workout(cur["scheduledId"])
            if cur.get("workoutId"):
                client.delete_workout(cur["workoutId"])
            state.pop(pid, None)
            result["removed"] += 1
            log(f"Rimosso {cur.get('name')}")
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"{pid}: rimozione {type(e).__name__}: {str(e)[:120]}")

    if not dry_run:
        store.write_json("workouts_sync.json", state)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--backup", help="percorso locale di DietaElia-backup.json")
    src.add_argument("--backup-graph", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    plan = load_plan_graph() if args.backup_graph else load_plan_local(args.backup)
    log(f"Piano letto: {len(plan)} sedute, {sum(1 for p in plan if p.get('struttura'))} con struttura")
    if not any(p.get("struttura") for p in plan) and not (LocalStore(args.dest).read_json("workouts_sync.json", {}) or {}):
        log("Niente da fare.")
        return 0
    client = login()
    res = sync(client, LocalStore(args.dest), plan, date.today(), args.dry_run)
    log(f"Creati {res['created']}, aggiornati {res['updated']}, rimossi {res['removed']}, errori {len(res['errors'])}")
    for e in res["errors"]:
        log("  ! " + e)
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
