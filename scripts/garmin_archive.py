"""
Archivio COMPLETO del profilo Garmin Connect (passato + presente), come JSON grezzo.

Uso (backfill di tutta la storia, riprendibile):
    python scripts/garmin_archive.py --dest "C:\\Users\\eliag\\OneDrive\\GarminData"

Uso incrementale (attivita' nuove + ultimi giorni), pensato per la GitHub Action:
    python scripts/garmin_archive.py --dest <cartella> --mode incremental

Al primo login chiede email/password nel terminale (oppure GARMIN_EMAIL/GARMIN_PASSWORD) e salva
il token in ~/.garminconnect (o $GARMINTOKENS): dalle volte successive non serve piu' nulla.

Layout della destinazione:
    profile/*.json                   profilo, dispositivi, zone, record, gear, obiettivi, ...
    history/<metrica>/<da>_<a>.json  serie a intervallo (peso, HRV, body battery, VO2max, ...)
    activities/list/<anno>.json      riepilogo completo di ogni attivita'
    activities/detail/<id>.json      dettaglio (splits, meteo, zone, serie temporali, GPS, ...)
    activities/fit/<id>.zip          file originale del dispositivo (secondo per secondo)
    daily/<anno>/<anno>-<mese>.json  metriche giornaliere (sonno, HRV, stress, passi, ...)
    state.json                       cosa e' gia' stato scaricato (per riprendere)
"""
import argparse
import getpass
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

TOKENSTORE = Path(os.environ.get("GARMINTOKENS", "~/.garminconnect")).expanduser()
RANGE_CHUNK_DAYS = 28
MAX_ERRORS_KEPT = 400

# metriche per singolo giorno: nome nel file -> (metodo, argomenti extra oltre alla data)
PER_DAY = {
    "stats": "get_stats",
    "sleep": "get_sleep_data",
    "hrv": "get_hrv_data",
    "restingHeartRate": "get_rhr_day",
    "heartRates": "get_heart_rates",
    "stress": "get_all_day_stress",
    "steps": "get_steps_data",
    "floors": "get_floors",
    "spo2": "get_spo2_data",
    "respiration": "get_respiration_data",
    "hydration": "get_hydration_data",
    "intensityMinutes": "get_intensity_minutes_data",
    "bodyBatteryEvents": "get_body_battery_events",
    "trainingReadiness": "get_training_readiness",
    "trainingStatus": "get_training_status",
    "maxMetrics": "get_max_metrics",
    "weighIns": "get_daily_weigh_ins",
    "allDayEvents": "get_all_day_events",
    "lifestyleLogging": "get_lifestyle_logging_data",
}

# serie a intervallo: nome -> (metodo, tipo argomenti)  "dates" = (start, end)
RANGES = {
    "dailySteps": ("get_daily_steps", "dates"),
    "sleepDaily": ("get_sleep_daily", "dates"),
    "restingHeartRateDaily": ("get_rhr_daily", "dates"),
    "caloriesDaily": ("get_calories_daily", "dates"),
    "hrv": ("get_hrv_data_range", "dates"),
    "bodyBattery": ("get_body_battery", "dates"),
    "bodyComposition": ("get_body_composition", "dates"),
    "weighIns": ("get_weigh_ins", "dates"),
    "maxMetrics": ("get_max_metrics_range", "dates"),
    "enduranceScore": ("get_endurance_score", "dates"),
    "hillScore": ("get_hill_score", "dates"),
    "racePredictions": ("get_race_predictions", "dates"),
    "runningTolerance": ("get_running_tolerance", "dates"),
    "ftp": ("get_functional_threshold_power_range", "dates"),
    "bloodPressure": ("get_blood_pressure", "dates"),
    "weeklyIntensityMinutes": ("get_weekly_intensity_minutes", "dates"),
}

# profilo: nome -> (metodo, argomenti posizionali)
PROFILE = {
    "userProfile": ("get_user_profile", ()),
    "userSettings": ("get_userprofile_settings", ()),
    "fullName": ("get_full_name", ()),
    "unitSystem": ("get_unit_system", ()),
    "devices": ("get_devices", ()),
    "primaryTrainingDevice": ("get_primary_training_device", ()),
    "deviceLastUsed": ("get_device_last_used", ()),
    "heartRateZones": ("get_heart_rate_zones", ()),
    "powerZones": ("get_power_zones", ()),
    "cyclingFtp": ("get_cycling_ftp", ()),
    "lactateThreshold": ("get_lactate_threshold", ()),
    "personalRecords": ("get_personal_record", ()),
    "earnedBadges": ("get_earned_badges", ()),
    "availableBadges": ("get_available_badges", ()),
    "inProgressBadges": ("get_in_progress_badges", ()),
    "goalsActive": ("get_goals", ("active",)),
    "goalsFuture": ("get_goals", ("future",)),
    "goalsPast": ("get_goals", ("past",)),
    "workouts": ("get_workouts", ()),
    "trainingPlans": ("get_training_plans", ()),
    "activityTypes": ("get_activity_types", ()),
    "nextScheduledWorkout": ("get_next_scheduled_workout", ()),
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class RateLimited(Exception):
    pass


class LocalStore:
    def __init__(self, root):
        self.root = Path(root)

    def _p(self, rel):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def read_json(self, rel, default=None):
        p = self.root / rel
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

    def write_json(self, rel, obj):
        p = self._p(rel)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, p)

    def write_bytes(self, rel, data):
        p = self._p(rel)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def exists(self, rel):
        return (self.root / rel).exists()


class Archive:
    def __init__(self, client, store, pause, deadline):
        self.c = client
        self.store = store
        self.pause = pause
        self.deadline = deadline
        self.state = store.read_json("state.json", {}) or {}
        self.state.setdefault("activities", {})   # id -> {"detail": bool, "fit": bool}
        self.state.setdefault("days", {})         # YYYY-MM-DD -> True
        self.state.setdefault("ranges", {})       # "metrica|start" -> True
        self.state.setdefault("errors", [])
        self.state.setdefault("listComplete", False)
        self._months = {}
        self._dirty = 0

    # ---------- infrastruttura ----------
    def save_state(self):
        self.state["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.state["errors"] = self.state["errors"][-MAX_ERRORS_KEPT:]
        self.store.write_json("state.json", self.state)

    def note_error(self, what, e):
        self.state["errors"].append(f"{datetime.now().isoformat(timespec='seconds')} {what}: {type(e).__name__}: {str(e)[:160]}")

    def out_of_time(self):
        return self.deadline is not None and time.time() > self.deadline

    def call(self, what, fn, *args, **kwargs):
        """Chiama l'API con pausa, retry e gestione 429. Ritorna (ok, dati)."""
        for attempt in range(4):
            time.sleep(self.pause)
            try:
                return True, fn(*args, **kwargs)
            except GarminConnectTooManyRequestsError as e:
                wait = 60 * (attempt + 1) * 2
                log(f"429 su {what}: attendo {wait}s (tentativo {attempt + 1}/4)")
                if attempt == 3:
                    raise RateLimited(what) from e
                time.sleep(wait)
            except GarminConnectAuthenticationError:
                raise
            except GarminConnectConnectionError as e:
                msg = str(e)
                if any(code in msg for code in ("404", "400", "204")):
                    return False, None  # nessun dato per quel giorno/risorsa
                self.note_error(what, e)
                time.sleep(3 * (attempt + 1))
            except Exception as e:  # dati non attesi dalla libreria: registrare e proseguire
                self.note_error(what, e)
                return False, None
        return False, None

    # ---------- profilo ----------
    def sync_profile(self):
        last = self.state.get("profileAt")
        if last and (time.time() - last) < 20 * 3600:
            return
        log("Profilo...")
        for name, (method, args) in PROFILE.items():
            fn = getattr(self.c, method, None)
            if fn is None:
                continue
            ok, data = self.call("profile." + name, fn, *args)
            if ok:
                self.store.write_json(f"profile/{name}.json", data)
        last_used = self.store.read_json("profile/deviceLastUsed.json", {}) or {}
        upn = last_used.get("userProfileNumber")
        if upn:
            ok, gear = self.call("profile.gear", self.c.get_gear, str(upn))
            if ok:
                self.store.write_json("profile/gear.json", gear)
                items = gear if isinstance(gear, list) else (gear or {}).get("gearDTOs") or []
                stats = {}
                for g in items if isinstance(items, list) else []:
                    uuid = g.get("uuid") if isinstance(g, dict) else None
                    if uuid:
                        ok2, st = self.call("gear.stats", self.c.get_gear_stats, uuid)
                        if ok2:
                            stats[uuid] = st
                if stats:
                    self.store.write_json("profile/gearStats.json", stats)
        self.state["profileAt"] = time.time()
        self.save_state()

    # ---------- attivita' ----------
    def sync_activity_list(self, full):
        log("Elenco attivita'...")
        per_year = {}
        start, page = 0, 100
        seen_existing = 0
        while True:
            ok, batch = self.call(f"activities[{start}]", self.c.get_activities, start, page)
            if not ok or not batch:
                if not batch and ok:
                    self.state["listComplete"] = True
                break
            for a in batch:
                st = (a.get("startTimeLocal") or "")[:4]
                if st:
                    per_year.setdefault(st, []).append(a)
            start += page
            if not full and start >= 200:
                break
            if self.out_of_time():
                break
        for year, items in per_year.items():
            rel = f"activities/list/{year}.json"
            merged = {str(x.get("activityId")): x for x in (self.store.read_json(rel, []) or [])}
            for x in items:
                merged[str(x.get("activityId"))] = x
            self.store.write_json(rel, sorted(merged.values(), key=lambda x: x.get("startTimeLocal", ""), reverse=True))
        for items in per_year.values():
            for a in items:
                self.state["activities"].setdefault(str(a.get("activityId")), {"detail": False, "fit": False, "date": (a.get("startTimeLocal") or "")[:10], "type": ((a.get("activityType") or {}).get("typeKey"))})
        log(f"  {len(self.state['activities'])} attivita' note")
        self.save_state()

    def sync_activity_details(self, want_fit):
        todo = sorted(
            [(aid, meta) for aid, meta in self.state["activities"].items() if not meta.get("detail") or (want_fit and not meta.get("fit"))],
            key=lambda kv: kv[1].get("date", ""), reverse=True)
        log(f"Dettagli attivita' da scaricare: {len(todo)}")
        for i, (aid, meta) in enumerate(todo, 1):
            if self.out_of_time():
                log("Tempo massimo raggiunto, mi fermo (riprendo al prossimo giro)")
                return False
            if not meta.get("detail"):
                d = {}
                calls = {
                    "summary": (self.c.get_activity, (aid,)),
                    "splits": (self.c.get_activity_splits, (aid,)),
                    "typedSplits": (self.c.get_activity_typed_splits, (aid,)),
                    "splitSummaries": (self.c.get_activity_split_summaries, (aid,)),
                    "weather": (self.c.get_activity_weather, (aid,)),
                    "hrZones": (self.c.get_activity_hr_in_timezones, (aid,)),
                    "gear": (self.c.get_activity_gear, (aid,)),
                    "details": (self.c.get_activity_details, (aid,)),
                }
                if meta.get("type") in ("cycling", "road_biking", "mountain_biking", "gravel_cycling", "indoor_cycling", "virtual_ride", "e_bike_fitness", "e_bike_mountain"):
                    calls["powerZones"] = (self.c.get_activity_power_in_timezones, (aid,))
                if meta.get("type") in ("strength_training", "hiit", "cardio_training"):
                    calls["exerciseSets"] = (self.c.get_activity_exercise_sets, (aid,))
                for k, (fn, args) in calls.items():
                    ok, data = self.call(f"activity.{aid}.{k}", fn, *args)
                    if ok:
                        d[k] = data
                if d:
                    self.store.write_json(f"activities/detail/{aid}.json", d)
                    meta["detail"] = True
            if want_fit and not meta.get("fit"):
                ok, data = self.call(f"activity.{aid}.fit", self.c.download_activity, aid, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)
                if ok and data:
                    self.store.write_bytes(f"activities/fit/{aid}.zip", data)
                    meta["fit"] = True
                elif ok is False:
                    meta["fit"] = None  # nessun file originale (es. attivita' manuale): non riprovare
            if i % 10 == 0:
                log(f"  attivita' {i}/{len(todo)} ({meta.get('date')})")
                self.save_state()
        self.save_state()
        return True

    # ---------- giorni ----------
    def _month_key(self, day):
        return day[:4], day[:7]

    def _month_get(self, day):
        year, ym = self._month_key(day)
        if ym not in self._months:
            self._months[ym] = self.store.read_json(f"daily/{year}/{ym}.json", {}) or {}
        return ym, self._months[ym]

    def flush_months(self):
        for ym, data in self._months.items():
            self.store.write_json(f"daily/{ym[:4]}/{ym}.json", data)
        self._months = {}

    def sync_day(self, day):
        ym, month = self._month_get(day)
        rec = month.setdefault(day, {})
        for name, method in PER_DAY.items():
            fn = getattr(self.c, method, None)
            if fn is None:
                continue
            ok, data = self.call(f"day.{day}.{name}", fn, day)
            if ok and data not in (None, [], {}):
                rec[name] = data
        self.state["days"][day] = True

    def sync_days(self, since, until, redo_last=0):
        days = []
        d = until
        while d >= since:
            k = d.isoformat()
            if not self.state["days"].get(k) or (until - d).days < redo_last:
                days.append(k)
            d -= timedelta(days=1)
        log(f"Giorni da scaricare: {len(days)}")
        prev_ym = None
        for i, k in enumerate(days, 1):
            if self.out_of_time():
                log("Tempo massimo raggiunto, mi fermo (riprendo al prossimo giro)")
                break
            self.sync_day(k)
            if prev_ym and k[:7] != prev_ym:
                self.flush_months()
            prev_ym = k[:7]
            if i % 15 == 0:
                self.flush_months()
                self.save_state()
                log(f"  giorno {i}/{len(days)} ({k})")
        self.flush_months()
        self.save_state()

    # ---------- serie a intervallo ----------
    def sync_ranges(self, since, until, recent_only=False):
        log("Serie storiche a intervallo...")
        end = until
        while end >= since:
            start = max(since, end - timedelta(days=RANGE_CHUNK_DAYS - 1))
            for name, (method, kind) in RANGES.items():
                fn = getattr(self.c, method, None)
                key = f"{name}|{start.isoformat()}"
                is_recent = (until - end).days < 40
                if fn is None or (self.state["ranges"].get(key) and not is_recent):
                    continue
                if self.out_of_time():
                    return
                ok, data = self.call(f"range.{name}.{start}", fn, start.isoformat(), end.isoformat())
                if ok:
                    if data not in (None, [], {}):
                        self.store.write_json(f"history/{name}/{start.isoformat()}_{end.isoformat()}.json", data)
                    self.state["ranges"][key] = True
            self.save_state()
            if recent_only:
                break
            end = start - timedelta(days=1)


def login():
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    have_tokens = TOKENSTORE.exists() and any(TOKENSTORE.iterdir())
    if not have_tokens and (not email or not password):
        if not sys.stdin.isatty():
            raise SystemExit("Nessun token salvato e nessuna credenziale: esegui una volta da un terminale.")
        email = input("Email Garmin Connect: ").strip()
        password = getpass.getpass("Password Garmin Connect: ")
    client = Garmin(email=email, password=password, prompt_mfa=lambda: input("Codice MFA: ").strip())
    client.login(str(TOKENSTORE))
    return client


def earliest_date(state):
    ds = [m.get("date") for m in state["activities"].values() if m.get("date")]
    return date.fromisoformat(min(ds)) if ds else date.today() - timedelta(days=365)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True)
    ap.add_argument("--mode", choices=["backfill", "incremental"], default="backfill")
    ap.add_argument("--since", help="YYYY-MM-DD: non andare piu' indietro di questa data")
    ap.add_argument("--recent-days", type=int, default=3, help="incremental: giorni da riscaricare sempre")
    ap.add_argument("--pause", type=float, default=0.6, help="secondi tra una chiamata e l'altra")
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--skip-fit", action="store_true")
    args = ap.parse_args()

    deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None
    try:
        client = login()
    except GarminConnectAuthenticationError as e:
        log(f"Autenticazione Garmin fallita: {e}")
        return 1
    except GarminConnectTooManyRequestsError as e:
        log(f"Garmin ha limitato il login, riprova piu' tardi: {e}")
        return 3

    arc = Archive(client, LocalStore(args.dest), args.pause, deadline)
    today = date.today()
    try:
        arc.sync_profile()
        arc.sync_activity_list(full=(args.mode == "backfill" and not arc.state["listComplete"]))
        floor = date.fromisoformat(args.since) if args.since else earliest_date(arc.state) - timedelta(days=30)
        if args.mode == "incremental":
            floor = max(floor, today - timedelta(days=args.recent_days + 30))
        arc.sync_activity_details(want_fit=not args.skip_fit)
        arc.sync_days(floor, today, redo_last=args.recent_days if args.mode == "incremental" else 1)
        arc.sync_ranges(floor, today, recent_only=(args.mode == "incremental"))
    except RateLimited as e:
        log(f"Garmin limita le richieste ({e}). Progressi salvati: rilancia piu' tardi per riprendere.")
        arc.flush_months(); arc.save_state()
        return 3
    except GarminConnectAuthenticationError as e:
        log(f"Autenticazione Garmin scaduta: {e}")
        arc.flush_months(); arc.save_state()
        return 1
    except KeyboardInterrupt:
        log("Interrotto: salvo i progressi.")
        arc.flush_months(); arc.save_state()
        return 130
    arc.flush_months()
    arc.save_state()
    n_det = sum(1 for m in arc.state["activities"].values() if m.get("detail"))
    log(f"FATTO. Attivita' {len(arc.state['activities'])} (dettaglio {n_det}), giorni {len(arc.state['days'])}, errori registrati {len(arc.state['errors'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
