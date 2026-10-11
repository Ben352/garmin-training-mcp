"""Synthetic Garmin Connect client: deterministic fake data in Garmin's raw API shapes.

With GARMIN_SYNTHETIC=1, `cli.login()` returns `SyntheticGarmin` instead of logging in,
so the whole pipeline (fetch -> parse -> DuckDB -> MCP server) runs with no Garmin
account. Responses use the same keys and quirks as the real API (°F weather, RPE x10,
local-millis sleep timestamps, placeholder values on unworn days), so the real parsing
code is exercised unchanged.

The athlete is fictional and every value is derived from a seeded hash of the date or
activity id: the same request always returns the same data. Planted patterns, designed
for the 2026-03-30 .. 2026-09-30 window:

- Fitness improves: easy pace ~6:00 -> ~5:30 /km, VO2 max ~46 -> ~50, resting HR
  ~55 -> ~50 bpm.
- 4-week blocks: volume builds ~45% over the window; every 4th week is a recovery week
  at ~60% volume with no hard sessions.
- Illness 2026-07-13 .. 07-19: no training, resting HR +6, HRV down, poor sleep.
- Heat: HR is ~0.5 bpm higher per °C above 15 °C at the same effort.
- Short sleep (< 6 h) raises that day's resting HR ~3 bpm and lowers HRV ~9 ms.
- A hard day (training load > 120) raises the next day's resting HR ~2 bpm.
- Half marathon race on Sunday 2026-09-27 after a taper week.

Data-quality traps the real data also has: unworn and partial-wear days, negative stress
sentinels, treadmill runs (no GPS, weather or elevation), strength sessions reporting
distance 0, and cycling activities the fetch filters out.
"""

import hashlib
import math
import random
from datetime import UTC, date, datetime, timedelta
from functools import cache, lru_cache
from itertools import pairwise
from typing import Any

from garminconnect import GarminConnectConnectionError

SEED = "garmin-mcp-synthetic-v1"
WINDOW_START = date(2026, 3, 30)
WINDOW_END = date(2026, 9, 30)
ILLNESS = (date(2026, 7, 13), date(2026, 7, 19))
RACE_DAY = date(2026, 9, 27)

MAX_HR = 190
MASS_KG = 70
BMR_KCAL = 1750
SAMPLE_S = 2.0
ZONE_LOW_BPM = [95, 114, 133, 152, 171]  # 50/60/70/80/90 % of max HR

# name, lat, lon, terrain amplitude (m)
ROUTES = {
    "flat": ("Riverside", 51.5072, -0.0877, 2.0),
    "rolling": ("Hill Park", 51.5712, -0.1466, 12.0),
    "canal": ("Canal Path", 51.5362, -0.0601, 3.0),
    "trail": ("Woodland Trails", 51.4402, -0.0512, 40.0),
}

SLOT_RUN, SLOT_STRENGTH, SLOT_OTHER = 1, 2, 3


# ---------------------------------------------------------------- helpers


def _rng(*key: object) -> random.Random:
    digest = hashlib.sha256(":".join(map(str, (SEED, *key))).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def fitness(d: date) -> float:
    """0 at the window start, 1 at the end."""
    span = (WINDOW_END - WINDOW_START).days
    return _clamp((d - WINDOW_START).days / span, 0.0, 1.0)


def ill(d: date) -> bool:
    return ILLNESS[0] <= d <= ILLNESS[1]


def utc_offset_h(d: date) -> int:
    """Europe/London-style DST, close enough for synthetic data."""
    return 1 if date(d.year, 3, 29) <= d < date(d.year, 10, 25) else 0


def activity_id(d: date, slot: int) -> int:
    return int(d.strftime("%Y%m%d")) * 100 + slot


def _decode_id(aid: int) -> tuple[date, int]:
    s = str(aid)
    return date(int(s[:4]), int(s[4:6]), int(s[6:8])), aid % 100


def _local_ms(dt: datetime) -> int:
    """Garmin 'local' epoch millis: local wall-clock time encoded as if it were UTC."""
    return int(dt.replace(tzinfo=UTC).timestamp() * 1000)


# ---------------------------------------------------------------- weather


@cache
def weather(d: date) -> dict[str, Any]:
    r = _rng("weather", d)
    doy = d.timetuple().tm_yday
    temp_c = 12 + 8 * math.sin(2 * math.pi * (doy - 110) / 365) + r.gauss(0, 3)
    rain = r.random() < 0.2
    desc = "Rain" if rain else r.choice(["Clear", "Partly Cloudy", "Mostly Cloudy"])
    return {
        "temp_c": round(temp_c, 1),
        "humidity_pct": round(_clamp(r.gauss(85 if rain else 68, 8), 35, 100)),
        "wind_mph": round(_clamp(r.gauss(8, 4), 0, 30)),
        "rain": rain,
        "desc": desc,
    }


# ---------------------------------------------------------------- training plan


def paces(d: date) -> dict[str, float]:
    """Target paces in s/km for the athlete's fitness on day d."""
    f = fitness(d)
    return {
        "easy": 360 - 30 * f,
        "tempo": 300 - 25 * f,
        "interval": 270 - 25 * f,
        "race": 285 - 25 * f,
        "threshold": 290 - 25 * f,
    }


def _volume(d: date) -> float:
    week = (d - WINDOW_START).days // 7
    vol = 1 + 0.45 * _clamp(week, 0, 20) / 20
    if week % 4 == 3:
        vol *= 0.6
    if RACE_DAY - timedelta(days=6) <= d < RACE_DAY:
        vol *= 0.55
    return vol


@cache
def plan(d: date) -> tuple[dict[str, Any], ...]:
    """The sessions done on day d (empty on rest days)."""
    if ill(d) or RACE_DAY < d <= RACE_DAY + timedelta(days=3):
        return ()
    if d == RACE_DAY:
        return ({"slot": SLOT_RUN, "kind": "race", "route": "flat"},)

    r = _rng("plan", d)
    week = (d - WINDOW_START).days // 7
    recovery_week = week % 4 == 3
    taper = RACE_DAY - timedelta(days=6) <= d < RACE_DAY
    sessions = []

    kind = {1: "intervals", 2: "easy", 3: "tempo", 5: "easy", 6: "long"}.get(
        d.weekday()
    )
    if kind in ("intervals", "tempo") and recovery_week:
        kind = "easy"
    if kind == "long" and taper:
        kind = None
    if kind and r.random() > 0.08:  # occasionally skipped
        route = r.choice(["flat", "rolling", "canal"])
        if d.weekday() == 5 and week % 2 == 1:
            route = "trail"
        treadmill = kind in ("intervals", "easy") and weather(d)["rain"]
        treadmill = treadmill and route != "trail" and r.random() < 0.7
        sessions.append(
            {
                "slot": SLOT_RUN,
                "kind": kind,
                "route": "treadmill" if treadmill else route,
            }
        )
    if d.weekday() in (2, 4) and not taper and r.random() > 0.15:
        sessions.append({"slot": SLOT_STRENGTH, "kind": "strength"})
    if d.weekday() == 5 and week % 6 == 2:
        sessions.append({"slot": SLOT_OTHER, "kind": "cycling"})
    return tuple(sessions)


def _segments(d: date, kind: str) -> list[tuple[float, float, str]]:
    """Run as (distance_m, target pace s/km, intensity type) segments. Auto-lapped
    runs are cut into 1 km segments so every lap ends exactly on a segment."""
    r = _rng("segments", d)
    p = {k: v + r.gauss(0, 4) for k, v in paces(d).items()}
    vol = _volume(d)

    def auto_lap(total_m: float, pace_at) -> list[tuple[float, float, str]]:
        out, done = [], 0.0
        while total_m - done > 1:
            step = min(1000.0, total_m - done)
            out.append((step, pace_at(done / total_m), "ACTIVE"))
            done += step
        return out

    if kind == "easy":
        return auto_lap(
            round(7000 * vol * r.uniform(0.85, 1.15), -1), lambda _: p["easy"]
        )
    if kind == "long":
        week = (d - WINDOW_START).days // 7
        fast_finish = week % 2 == 0

        def long_pace(frac: float) -> float:
            return p["easy"] - 15 if fast_finish and frac > 0.8 else p["easy"] + 10

        return auto_lap(round(14000 * vol * r.uniform(0.95, 1.05), -2), long_pace)
    if kind == "race":
        # Even pacing, fading ~6 s/km over the last 4 km.
        return auto_lap(21097.5, lambda frac: p["race"] + (6 if frac > 0.81 else 0))
    if kind == "tempo":
        tempo_m = round(5000 * min(vol, 1.3), -2)
        return [
            (2000.0, p["easy"], "WARMUP"),
            (tempo_m, p["tempo"], "INTERVAL"),
            (2000.0, p["easy"] + 15, "COOLDOWN"),
        ]
    if kind == "intervals":
        reps = 5 + round(3 * fitness(d))
        segs = [(2000.0, p["easy"], "WARMUP")]
        for i in range(reps):
            segs.append((800.0, p["interval"], "INTERVAL"))
            if i < reps - 1:
                segs.append((400.0, p["easy"] + 60, "RECOVERY"))
        segs.append((1500.0, p["easy"] + 15, "COOLDOWN"))
        return segs
    raise ValueError(kind)


# ---------------------------------------------------------------- daily state


@cache
def _latent(d: date) -> dict[str, Any]:
    """Body state for day d, whether or not the watch recorded it."""
    r = _rng("latent", d)
    if ill(d):
        sleep_h = r.gauss(6.0, 0.6)
    elif r.random() < 0.12:
        sleep_h = r.uniform(4.8, 5.9)
    else:
        sleep_h = r.gauss(7.1, 0.5) + (0.5 if d.weekday() >= 5 else 0)
    sleep_h = _clamp(sleep_h, 4.5, 9.5)
    short = sleep_h < 6
    load_yesterday = sum(
        _run(activity_id(d - timedelta(days=1), s["slot"]))["load"]
        for s in plan(d - timedelta(days=1))
        if s["slot"] == SLOT_RUN
    )
    f = fitness(d)
    rhr = (
        55
        - 5 * f
        + (3 if short else 0)
        + (6 if ill(d) else 0)
        + (2 if load_yesterday > 120 else 0)
        + r.gauss(0, 1.2)
    )
    hrv = (
        42
        + 14 * f
        - (9 if short else 0)
        - (14 if ill(d) else 0)
        - (4 if load_yesterday > 120 else 0)
        + r.gauss(0, 3)
    )
    return {"sleep_h": sleep_h, "rhr": rhr, "hrv": hrv}


@cache
def _wear(d: date) -> dict[str, Any]:
    r = _rng("wear", d)
    u = r.random()
    status = "unworn" if u < 0.12 else "partial" if u < 0.24 else "full"
    if ill(d) or any(s["slot"] == SLOT_RUN for s in plan(d)):
        status = "full"  # runs are always recorded
    return {
        "status": status,
        "sleep_tracked": status == "full" and r.random() < 0.65,
        "awake_s": int(r.uniform(13, 16) * 3600)
        if status == "full"
        else int(r.uniform(1, 3) * 3600),
    }


def rest_hr(d: date) -> float:
    return _latent(d)["rhr"]


# ---------------------------------------------------------------- run simulation


def _elevation(route: str, dist: float) -> float | None:
    if route == "treadmill":
        return None
    amp = ROUTES[route][3]
    return 30 + amp * (math.sin(dist / 700) + 0.5 * math.sin(dist / 260 + 1))


def _grade_pct(route: str, dist: float) -> float:
    if route == "treadmill":
        return 0.0
    amp = ROUTES[route][3]
    return (
        100 * amp * (math.cos(dist / 700) / 700 + 0.5 * math.cos(dist / 260 + 1) / 260)
    )


def _run_start_local(d: date, kind: str) -> datetime:
    r = _rng("start", d)
    if kind == "race":
        return datetime(d.year, d.month, d.day, 9, 0, 0)
    hour = 8 if d.weekday() >= 5 else 7
    return datetime(d.year, d.month, d.day, hour, r.randint(0, 45), r.randint(0, 59))


@lru_cache(maxsize=256)
def _run(aid: int) -> dict[str, Any]:
    """Simulate one run at 2 s resolution and build every API response for it."""
    d, _ = _decode_id(aid)
    session = next((s for s in plan(d) if s["slot"] == SLOT_RUN), None)
    if session is None:
        raise GarminConnectConnectionError(f"Activity {aid} not found")
    kind, route = session["kind"], session["route"]
    r = _rng("run", aid)
    treadmill = route == "treadmill"
    temp_c = None if treadmill else weather(d)["temp_c"] - 2  # mornings are cooler
    rest = rest_hr(d)
    thr_speed = 1000 / paces(d)["threshold"]
    start_local = _run_start_local(d, kind)
    start_gmt = start_local - timedelta(hours=utc_offset_h(d))

    samples: list[dict[str, Any]] = []
    t = dist = 0.0
    hr = rest + 25
    segments = _segments(d, kind)
    seg_bounds = []
    for seg_dist, seg_pace, _ in segments:
        seg_start_idx, seg_end = len(samples), dist + seg_dist
        while dist < seg_end - 1e-6:
            grade = _grade_pct(route, dist)
            speed = 1000 / seg_pace / (1 + 0.033 * grade) * (1 + r.gauss(0, 0.02))
            step = min(speed * SAMPLE_S, seg_end - dist)
            dt = step / speed
            dist += step
            t += dt
            gap_speed = speed * (1 + 0.033 * grade)
            ratio = gap_speed / thr_speed
            target = rest + (MAX_HR - rest) * min(0.97, 0.88 * ratio**2)
            target += 0.5 * max(0.0, (temp_c or 15) - 15)  # heat
            target += 0.04 * max(0.0, t / 60 - 20)  # cardiac drift
            hr += (target - hr) * (1 - math.exp(-dt / 25))
            cadence = _clamp(150 + 12 * (speed - 2.5), 148, 192) + r.gauss(0, 1.5)
            elev = _elevation(route, dist)
            samples.append(
                {
                    "directTimestamp": int(
                        (start_gmt.replace(tzinfo=UTC).timestamp() + t) * 1000
                    ),
                    "sumDistance": round(dist, 2),
                    "sumDuration": round(t, 2),
                    "directSpeed": round(speed, 3),
                    "directGradeAdjustedSpeed": round(gap_speed, 3),
                    "directHeartRate": round(hr + r.gauss(0, 1.5)),
                    "directDoubleCadence": round(cadence, 1),
                    "directPower": round(1.25 * MASS_KG * speed * (1 + 0.04 * grade)),
                    "directElevation": None
                    if elev is None
                    else round(elev + r.gauss(0, 0.3), 1),
                    "_true_elev": elev,
                }
            )
        seg_bounds.append((seg_start_idx, len(samples)))

    def stats(chunk: list[dict[str, Any]], t0: float) -> dict[str, Any]:
        hrs = [s["directHeartRate"] for s in chunk]
        cad = [s["directDoubleCadence"] for s in chunk]
        pw = [s["directPower"] for s in chunk]
        elev = [s["_true_elev"] for s in chunk]
        dur = chunk[-1]["sumDuration"] - t0
        gain = loss = None
        if elev[0] is not None:
            diffs = [b - a for a, b in pairwise(elev)]
            gain = sum(x for x in diffs if x > 0)
            loss = -sum(x for x in diffs if x < 0)
        speed = sum(s["directSpeed"] for s in chunk) / len(chunk)
        stride_cm = speed / (sum(cad) / len(cad) / 60) * 100
        vo_cm = 8.5 + r.gauss(0, 0.3)
        return {
            "duration": round(dur, 1),
            "movingDuration": round(dur, 1),
            "averageHR": round(sum(hrs) / len(hrs)),
            "maxHR": max(hrs),
            "minHR": min(hrs),
            "averageRunCadence": round(sum(cad) / len(cad), 1),
            "maxRunCadence": round(max(cad), 1),
            "averagePower": round(sum(pw) / len(pw)),
            "maxPower": max(pw),
            "normalizedPower": round((sum(p**4 for p in pw) / len(pw)) ** 0.25),
            "elevationGain": None if gain is None else round(gain, 1),
            "elevationLoss": None if loss is None else round(loss, 1),
            "avgGradeAdjustedSpeed": round(
                sum(s["directGradeAdjustedSpeed"] for s in chunk) / len(chunk), 3
            ),
            "averageSpeed": round(speed, 3),
            "groundContactTime": round(300 - 20 * speed + r.gauss(0, 3), 1),
            "strideLength": round(stride_cm, 1),
            "verticalOscillation": round(vo_cm, 1),
            "verticalRatio": round(100 * vo_cm / stride_cm, 1),
        }

    laps = []
    prev_t = prev_d = 0.0
    for i, ((lo, hi), (seg_dist, _, intensity)) in enumerate(
        zip(seg_bounds, segments), 1
    ):
        chunk = samples[lo:hi]
        lap = stats(chunk, prev_t)
        lap |= {
            "lapIndex": i,
            "intensityType": intensity,
            "startTimeGMT": (start_gmt + timedelta(seconds=prev_t)).strftime(
                "%Y-%m-%dT%H:%M:%S.0"
            ),
            "distance": round(chunk[-1]["sumDistance"] - prev_d, 2),
        }
        laps.append(lap)
        prev_t, prev_d = chunk[-1]["sumDuration"], chunk[-1]["sumDistance"]

    # HR zones and training load (TRIMP-style), from the samples.
    zones = [0.0] * 5
    load = 0.0
    prev = 0.0
    for s in samples:
        dt = s["sumDuration"] - prev
        prev = s["sumDuration"]
        z = sum(s["directHeartRate"] >= b for b in ZONE_LOW_BPM[1:])
        zones[z] += dt
        hrr = _clamp((s["directHeartRate"] - rest) / (MAX_HR - rest), 0, 1)
        load += dt / 60 * hrr * 0.64 * math.exp(1.92 * hrr)
    load = round(load, 1)
    aerobic_te = round(min(5.0, 1.0 + load / 40), 1)
    anaerobic_te = round(min(5.0, zones[4] / 60 * 0.25 + zones[3] / 60 * 0.03), 1)
    label = {
        "intervals": "VO2MAX",
        "tempo": "TEMPO",
        "race": "LACTATE_THRESHOLD",
        "long": "AEROBIC_BASE",
    }.get(kind, "RECOVERY" if load < 35 else "AEROBIC_BASE")

    summary = stats(samples, 0.0)
    total_dist = samples[-1]["sumDistance"]
    total_t = samples[-1]["sumDuration"]
    steps = round(sum(s["directDoubleCadence"] for s in samples) * SAMPLE_S / 60)
    rated = r.random() < 0.7
    effort = {"easy": 3, "long": 5, "tempo": 7, "intervals": 8, "race": 9}[kind]
    summary |= {
        "startTimeLocal": start_local.strftime("%Y-%m-%dT%H:%M:%S.0"),
        "startTimeGMT": start_gmt.strftime("%Y-%m-%dT%H:%M:%S.0"),
        "distance": round(total_dist, 2),
        "elapsedDuration": round(total_t + r.uniform(0, 90), 1),
        "averageMovingSpeed": round(total_dist / total_t, 3),
        "maxSpeed": max(s["directSpeed"] for s in samples),
        "minElevation": None
        if treadmill
        else round(min(s["_true_elev"] for s in samples), 1),
        "maxElevation": None
        if treadmill
        else round(max(s["_true_elev"] for s in samples), 1),
        "trainingEffect": aerobic_te,
        "anaerobicTrainingEffect": anaerobic_te,
        "trainingEffectLabel": label,
        "activityTrainingLoad": load,
        # Feel 0/25/50/75/100, RPE stored x10: same encoding as the real API.
        "directWorkoutFeel": r.choice([50, 75, 75, 100]) if rated else None,
        "directWorkoutRpe": _clamp(effort + r.randint(-1, 1), 1, 10) * 10
        if rated
        else None,
        "differenceBodyBattery": -round(load / 8 + 3),
        "steps": steps,
    }

    route_name = "Treadmill" if treadmill else ROUTES[route][0]
    type_key = {"treadmill": "treadmill_running", "trail": "trail_running"}.get(
        route, "running"
    )
    name = "Riverside Half Marathon" if kind == "race" else f"{route_name} Running"
    lat = lon = end_lat = end_lon = None
    if not treadmill:
        lat, lon = ROUTES[route][1], ROUTES[route][2]
        end_lat, end_lon = (
            round(lat + r.uniform(-0.002, 0.002), 5),
            round(lon + r.uniform(-0.002, 0.002), 5),
        )
    listing = {
        "activityId": aid,
        "activityName": name,
        "activityType": {"typeKey": type_key},
        "locationName": None if treadmill else route_name,
        "startLatitude": lat,
        "startLongitude": lon,
        "endLatitude": end_lat,
        "endLongitude": end_lon,
        "startTimeLocal": start_local.strftime("%Y-%m-%d %H:%M:%S"),
        "startTimeGMT": start_gmt.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": summary["duration"],
        "movingDuration": summary["movingDuration"],
        "distance": summary["distance"],
        "averageHR": summary["averageHR"],
        "maxHR": summary["maxHR"],
        "elevationGain": summary["elevationGain"],
        "calories": round(1.0 * MASS_KG * total_dist / 1000),
        "averageRunningCadenceInStepsPerMinute": summary["averageRunCadence"],
        "aerobicTrainingEffect": aerobic_te,
        "anaerobicTrainingEffect": anaerobic_te,
        "activityTrainingLoad": load,
    }

    keys = [
        "directTimestamp",
        "sumDistance",
        "sumDuration",
        "directSpeed",
        "directGradeAdjustedSpeed",
        "directHeartRate",
        "directDoubleCadence",
        "directPower",
        "directElevation",
    ]
    order = keys[:]
    _rng("descriptor-order", aid).shuffle(order)  # parsers must map by metricsIndex
    details = {
        "metricDescriptors": [{"key": k, "metricsIndex": order.index(k)} for k in keys],
        "activityDetailMetrics": [{"metrics": [s[k] for k in order]} for s in samples],
    }
    w = weather(d)
    return {
        "listing": listing,
        "activity": {"activityId": aid, "summaryDTO": summary},
        "splits": {"activityId": aid, "lapDTOs": laps},
        "hr_zones": [
            {"zoneNumber": i + 1, "secsInZone": round(s, 1), "zoneLowBoundary": b}
            for i, (s, b) in enumerate(zip(zones, ZONE_LOW_BPM))
        ],
        # The real endpoint reports °F and (apparently) mph; indoor runs get nothing.
        "weather": None
        if treadmill
        else {
            "temp": round(temp_c * 9 / 5 + 32),
            "apparentTemp": round((temp_c - w["wind_mph"] * 0.1) * 9 / 5 + 32),
            "relativeHumidity": w["humidity_pct"],
            "windSpeed": w["wind_mph"],
            "weatherTypeDTO": {"desc": w["desc"]},
        },
        "details": details,
        "load": load,
        "zones_s": zones,
        "kind": kind,
    }


def _other_listing(d: date, session: dict[str, Any]) -> dict[str, Any]:
    aid = activity_id(d, session["slot"])
    r = _rng("other", aid)
    if session["kind"] == "strength":
        start = datetime(d.year, d.month, d.day, 18, r.randint(0, 30), 0)
        duration = r.uniform(2400, 3600)
        sets = r.randint(15, 22)
        return {
            "activityId": aid,
            "activityName": "Strength",
            "activityType": {"typeKey": "strength_training"},
            "locationName": None,
            "startLatitude": None,
            "startLongitude": None,
            "endLatitude": None,
            "endLongitude": None,
            "startTimeLocal": start.strftime("%Y-%m-%d %H:%M:%S"),
            "startTimeGMT": (start - timedelta(hours=utc_offset_h(d))).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "duration": round(duration, 1),
            "movingDuration": round(duration * 0.4, 1),
            "distance": 0.0,  # the real API reports 0, not null
            "averageHR": r.randint(98, 115),
            "maxHR": r.randint(135, 155),
            "elevationGain": None,
            "calories": round(duration / 60 * 6),
            "averageRunningCadenceInStepsPerMinute": None,
            "aerobicTrainingEffect": round(r.uniform(1.0, 1.8), 1),
            "anaerobicTrainingEffect": round(r.uniform(0.5, 1.5), 1),
            "activityTrainingLoad": round(r.uniform(18, 40), 1),
            "totalSets": sets,
            "activeSets": sets - 3,
            "totalReps": r.randint(120, 200),
        }
    start = datetime(d.year, d.month, d.day, 14, 0, 0)
    return {
        "activityId": aid,
        "activityName": "Road Cycling",
        "activityType": {"typeKey": "cycling"},
        "startTimeLocal": start.strftime("%Y-%m-%d %H:%M:%S"),
        "startTimeGMT": (start - timedelta(hours=utc_offset_h(d))).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "duration": 5400.0,
        "distance": 40000.0,
    }


def listings(d: date) -> list[dict[str, Any]]:
    out = []
    for s in plan(d):
        if s["slot"] == SLOT_RUN:
            out.append(_run(activity_id(d, s["slot"]))["listing"])
        else:
            out.append(_other_listing(d, s))
    return out


# ---------------------------------------------------------------- daily responses


def _hrv_7d(d: date) -> float | None:
    nights = [
        _latent(d - timedelta(days=i))["hrv"]
        for i in range(7)
        if _wear(d - timedelta(days=i))["sleep_tracked"]
    ]
    return round(sum(nights) / len(nights), 1) if len(nights) >= 4 else None


def user_summary(d: date) -> dict[str, Any]:
    r = _rng("summary", d)
    wear, lat = _wear(d), _latent(d)
    base = {"calendarDate": d.isoformat()}
    if wear["status"] == "unworn":
        # Garmin still returns goals, BMR calories and zeros for unworn days.
        return base | {
            "totalSteps": None,
            "dailyStepGoal": 8000,
            "totalDistanceMeters": 0,
            "activeKilocalories": 0.0,
            "totalKilocalories": float(BMR_KCAL),
            "moderateIntensityMinutes": 0,
            "vigorousIntensityMinutes": 0,
            "floorsAscended": 0.0,
            "highlyActiveSeconds": 0,
            "activeSeconds": 0,
            "sedentarySeconds": 86400,
            "averageStressLevel": -1,
            "maxStressLevel": -1,
            "measurableAwakeDuration": 0,
            "measurableAsleepDuration": 0,
        }
    if wear["status"] == "partial":
        steps = r.randint(100, 480)
        return base | {
            "totalSteps": steps,
            "dailyStepGoal": 8000,
            "totalDistanceMeters": round(steps * 0.78),
            "activeKilocalories": round(steps * 0.04, 1),
            "totalKilocalories": round(BMR_KCAL + steps * 0.04, 1),
            # Too little wear for a real resting HR: Garmin reports a bogus one.
            "restingHeartRate": r.randint(82, 100),
            "minHeartRate": r.randint(70, 85),
            "maxHeartRate": r.randint(100, 125),
            "averageStressLevel": -2,
            "maxStressLevel": -2,
            "moderateIntensityMinutes": 0,
            "vigorousIntensityMinutes": 0,
            "floorsAscended": round(r.uniform(0, 2), 5),
            "highlyActiveSeconds": 0,
            "activeSeconds": r.randint(300, 1200),
            "sedentarySeconds": wear["awake_s"],
            "measurableAwakeDuration": wear["awake_s"],
            "measurableAsleepDuration": 0,
        }

    runs = [_run(activity_id(d, s["slot"])) for s in plan(d) if s["slot"] == SLOT_RUN]
    others = [_other_listing(d, s) for s in plan(d) if s["slot"] != SLOT_RUN]
    run_steps = sum(x["activity"]["summaryDTO"]["steps"] for x in runs)
    walk_steps = max(2500, round(r.gauss(7500, 2000)))
    steps = walk_steps + run_steps + (1500 if others else 0)
    vigorous = round(sum(sum(x["zones_s"][2:]) for x in runs) / 60)
    moderate = round(sum(sum(x["zones_s"][:2]) for x in runs) / 60) + r.randint(10, 40)
    activity_kcal = sum(x["listing"]["calories"] for x in runs) + sum(
        o["calories"] for o in others if o["activityType"]["typeKey"] != "cycling"
    )
    active_kcal = round(300 + walk_steps * 0.04 + activity_kcal, 1)
    stress = r.gauss(30, 6) + (12 if ill(d) else 0) + (5 if lat["sleep_h"] < 6 else 0)
    no_stress = r.random() < 0.04
    sleep_s = int(lat["sleep_h"] * 3600) if wear["sleep_tracked"] else 0
    highly_active = vigorous * 60 + r.randint(0, 900)
    active = moderate * 60 + r.randint(1800, 5400)
    rhr7 = [
        _latent(d - timedelta(days=i))["rhr"]
        for i in range(7)
        if _wear(d - timedelta(days=i))["status"] == "full"
    ]
    bb_high = round(
        _clamp(
            92
            - (8 if lat["sleep_h"] < 6 else 0)
            - (20 if ill(d) else 0)
            + r.gauss(0, 4),
            30,
            100,
        )
    )
    bb_low = round(_clamp(r.gauss(18, 6), 5, bb_high - 10))
    return base | {
        "totalSteps": steps,
        "dailyStepGoal": 8000,
        "totalDistanceMeters": round(
            walk_steps * 0.78
            + sum(x["activity"]["summaryDTO"]["distance"] for x in runs)
        ),
        "activeKilocalories": active_kcal,
        "totalKilocalories": round(active_kcal + BMR_KCAL, 1),
        "restingHeartRate": round(lat["rhr"]),
        "lastSevenDaysAvgRestingHeartRate": round(sum(rhr7) / len(rhr7)),
        "minHeartRate": round(lat["rhr"]) - r.randint(2, 5),
        "maxHeartRate": max(
            [r.randint(115, 140)] + [x["activity"]["summaryDTO"]["maxHR"] for x in runs]
        ),
        "averageStressLevel": -1 if no_stress else round(_clamp(stress, 5, 90)),
        "maxStressLevel": -1
        if no_stress
        else round(_clamp(stress + r.uniform(40, 55), 20, 100)),
        "bodyBatteryHighestValue": bb_high,
        "bodyBatteryLowestValue": bb_low,
        "bodyBatteryChargedValue": round(_clamp(bb_high - 20 + r.gauss(0, 5), 10, 90)),
        "bodyBatteryDrainedValue": bb_high - bb_low + r.randint(0, 15),
        "moderateIntensityMinutes": moderate,
        "vigorousIntensityMinutes": vigorous,
        "floorsAscended": round(_clamp(r.gauss(8, 3), 0, 30), 5),
        "highlyActiveSeconds": highly_active,
        "activeSeconds": active,
        "sedentarySeconds": max(0, wear["awake_s"] - highly_active - active),
        "avgWakingRespirationValue": round(r.uniform(13.5, 16.0), 1),
        "measurableAwakeDuration": wear["awake_s"],
        "measurableAsleepDuration": sleep_s,
    }


def sleep_values(d: date) -> dict[str, Any] | None:
    """Sleep ending on the morning of d, or None if the watch wasn't worn overnight."""
    if not _wear(d)["sleep_tracked"]:
        return None
    r = _rng("sleep", d)
    lat = _latent(d)
    total = int(lat["sleep_h"] * 3600)
    deep = int(total * _clamp(r.gauss(0.17, 0.03), 0.08, 0.25))
    rem = int(total * _clamp(r.gauss(0.22, 0.03), 0.12, 0.3))
    awake = r.randint(2400, 4200) if ill(d) else r.randint(600, 2400)
    score = round(
        _clamp(
            45
            + 9 * (lat["sleep_h"] - 5)
            + 0.3 * (lat["hrv"] - 45)
            - (10 if ill(d) else 0)
            + r.gauss(0, 4),
            25,
            97,
        )
    )
    quality = (
        "EXCELLENT"
        if score >= 90
        else "GOOD"
        if score >= 80
        else "FAIR"
        if score >= 60
        else "POOR"
    )
    wake = datetime(d.year, d.month, d.day, 6, 30) + timedelta(
        minutes=r.randint(0, 60) + (45 if d.weekday() >= 5 else 0)
    )
    start = wake - timedelta(seconds=total + awake)
    hrv7 = _hrv_7d(d)
    return {
        "localSleepStartTimeInMillis": _local_ms(start),
        "localSleepEndTimeInMillis": _local_ms(wake),
        "totalSleepTimeInSeconds": total,
        "deepTime": deep,
        "remTime": rem,
        "lightTime": total - deep - rem,
        "awakeTime": awake,
        "sleepScore": score,
        "sleepScoreQuality": quality,
        "avgHeartRate": round(lat["rhr"] + r.uniform(2, 5), 1),
        "respiration": round(r.uniform(13.0, 15.5), 1),
        "bodyBatteryChange": round(score * 0.65),
        "avgOvernightHrv": round(lat["hrv"], 1),
        "hrv7dAverage": hrv7,
        "hrvStatus": "LOW" if ill(d) else "BALANCED" if hrv7 else "NONE",
    }


def vo2max(d: date) -> float:
    r = _rng("vo2", d)
    after_illness = ILLNESS[0] <= d <= ILLNESS[1] + timedelta(days=14)
    return 46 + 4 * fitness(d) - (0.6 if after_illness else 0) + r.gauss(0, 0.1)


# ---------------------------------------------------------------- client


def _dates(start: str, end: str) -> list[date]:
    s, e = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
    return [s + timedelta(days=i) for i in range((e - s).days + 1)]


class SyntheticGarmin:
    """Drop-in for the `garminconnect.Garmin` methods the fetch commands call."""

    def get_activities_by_date(
        self, startdate: str, enddate: str, activitytype=None, sortorder=None
    ) -> list[dict[str, Any]]:
        out = [a for d in _dates(startdate, enddate) for a in listings(d)]
        return out if sortorder == "asc" else out[::-1]

    def get_activity(self, activity_id: str) -> dict[str, Any]:
        return _run(int(activity_id))["activity"]

    def get_activity_splits(self, activity_id: str) -> dict[str, Any]:
        return _run(int(activity_id))["splits"]

    def get_activity_hr_in_timezones(self, activity_id: str) -> list[dict[str, Any]]:
        return _run(int(activity_id))["hr_zones"]

    def get_activity_weather(self, activity_id: str) -> dict[str, Any] | None:
        return _run(int(activity_id))["weather"]

    def get_activity_details(
        self, activity_id: str, maxchart: int = 2000, maxpoly: int = 4000
    ) -> dict[str, Any]:
        return _run(int(activity_id))["details"]

    def get_user_summary(self, cdate: str) -> dict[str, Any]:
        return user_summary(date.fromisoformat(cdate))

    def get_sleep_daily(self, startdate: str, enddate: str) -> list[dict[str, Any]]:
        out = []
        for d in _dates(startdate, enddate):
            values = sleep_values(d)
            if values is not None:
                out.append({"calendarDate": d.isoformat(), "values": values})
        return out

    def get_max_metrics_range(
        self, startdate: str, enddate: str
    ) -> list[dict[str, Any]]:
        """Garmin only reports VO2 max on days it changed, after qualifying outdoor
        runs."""
        out, last = [], None
        for d in _dates(startdate, enddate):
            qualifies = any(
                s["slot"] == SLOT_RUN
                and s["route"] != "treadmill"
                and s["kind"] in ("tempo", "intervals", "long", "race")
                for s in plan(d)
            )
            if not qualifies:
                continue
            value = round(vo2max(d), 1)
            if value != last:
                out.append(
                    {
                        "generic": {
                            "calendarDate": d.isoformat(),
                            "vo2MaxValue": float(round(value)),
                            "vo2MaxPreciseValue": value,
                        }
                    }
                )
                last = value
        return out
