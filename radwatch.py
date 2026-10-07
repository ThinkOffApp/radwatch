#!/usr/bin/env python3
"""radwatch - continuous logging and spectrum analysis for a RadiaCode 10x.

Three separable pieces, deliberately:

  log      poll the device, append dose rate and count rate to SQLite, store a spectrum every N minutes
  analyse  peak-find a stored spectrum, convert channels to keV with the device's own calibration,
           and match peaks against a gamma line table
  watch    Poisson alerting on count rate against a rolling baseline
  status   one JSON object for dashboards: latest reading, device battery/temperature/dose, watch verdict
  explain  the local model turns what watch and analyse computed into one paragraph

Nothing here asks a language model. Counting statistics are Poisson and the right test is arithmetic;
an LLM would be slower and worse at it. The model's job is the language layer on top: explaining a
spectrum, writing the daily summary, answering "was anything odd last week". That lives elsewhere and
reads this database.

Usage:
  radwatch.py log    [--serial SN | --bt MAC] [--db PATH] [--spectrum-every SEC]
  radwatch.py analyse [--db PATH] [--at ISO8601]
  radwatch.py status [--db PATH]
  radwatch.py selftest
"""
import argparse, json, math, os, sqlite3, sys, time, urllib.request
from datetime import datetime, timezone

# Home Assistant MQTT discovery. HA creates the entities itself from these, so there is no custom
# component to write or keep working across HA releases.
HA_DISCOVERY_PREFIX = "homeassistant"
STATE_TOPIC_FMT = "radwatch/{serial}/state"   # per device: one topic per box, never shared
SENSORS = [
    # key, name, unit, device_class, state_class, icon
    ("dose_rate", "Dose rate", "\u00b5Sv/h", None, "measurement", "mdi:radioactive"),
    ("count_rate", "Count rate", "cps", None, "measurement", "mdi:counter"),
    ("dose_rate_err", "Dose rate error", "%", None, "measurement", "mdi:plus-minus-variant"),
]


# Device protocol units -> micro-units. radiacode documents dose_rate and the accumulated dose only as
# "device protocol units"; the MQTT path has always multiplied by 1e6 to get uSv/h. ONE constant so
# the dashboard and Home Assistant can never disagree. Unverified until the device has arrived and
# been read side by side with its own display.
TO_MICRO = 1e6


def mqtt_connect(host, port, serial):
    """One client per device.

    The client id MUST be per-device. MQTT brokers enforce unique client ids and disconnect the
    existing session when a second client claims the same one, so a hardcoded "radwatch" would
    have had the Pi, the Ventuno and the VTA kicking each other off the broker in a loop, whatever
    their topics were (codexmb, 4 Oct).

    The last will is what actually makes the entities go unavailable if this process dies. Without
    it the broker never publishes "offline" and Home Assistant shows a stale reading as live
    forever, which is worse than showing nothing.
    """
    import paho.mqtt.client as mqtt
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"radwatch-{serial}")
    c.will_set(f"radwatch/{serial}/status", "offline", retain=True)
    c.connect(host, port, keepalive=60)
    c.loop_start()
    return c


def state_topic(serial):
    return STATE_TOPIC_FMT.format(serial=serial)


def mqtt_announce(client, serial):
    """One retained discovery message per sensor. HA picks them up with no configuration."""
    device = {"identifiers": [f"radwatch_{serial}"], "name": f"RadiaCode {serial}",
              "manufacturer": "RadiaCode", "model": "10x", "via_device": "radwatch"}
    for key, name, unit, dev_class, state_class, icon in SENSORS:
        uid = f"radwatch_{serial}_{key}"
        cfg = {"name": name, "unique_id": uid, "state_topic": state_topic(serial),
               "value_template": "{{ value_json." + key + " }}",
               "unit_of_measurement": unit, "state_class": state_class,
               "icon": icon, "device": device,
               "availability_topic": f"radwatch/{serial}/status"}
        if dev_class:
            cfg["device_class"] = dev_class
        client.publish(f"{HA_DISCOVERY_PREFIX}/sensor/{uid}/config", json.dumps(cfg), retain=True)
    client.publish(f"radwatch/{serial}/status", "online", retain=True)

# Gamma lines, keV. PROVENANCE: these are the standard natural-background and common-source lines.
# Check each against a published table (IAEA / LNHB) before any number from here goes in public.
LINES = [
    (59.54, "Am-241", "smoke detector source"),
    (238.6, "Pb-212", "Th-232 chain"),
    (295.2, "Pb-214", "Ra-226 chain"),
    (351.9, "Pb-214", "Ra-226 chain"),
    (511.0, "annihilation", "positron, or cosmic"),
    (583.2, "Tl-208", "Th-232 chain"),
    (609.3, "Bi-214", "Ra-226 chain"),
    (661.657, "Cs-137", "fallout or a check source"),
    (911.2, "Ac-228", "Th-232 chain"),
    (1173.2, "Co-60", "industrial source"),
    (1332.5, "Co-60", "industrial source"),
    (1460.8, "K-40", "natural, in soil, bananas, salt substitute"),
    (1764.5, "Bi-214", "Ra-226 chain"),
    (2614.5, "Tl-208", "Th-232 chain, the highest common natural line"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS reading(
  ts TEXT PRIMARY KEY, count_rate REAL, count_rate_err REAL, dose_rate REAL, dose_rate_err REAL, flags INT);
CREATE TABLE IF NOT EXISTS spectrum(
  ts TEXT PRIMARY KEY, duration_s REAL, a0 REAL, a1 REAL, a2 REAL, counts TEXT);
CREATE TABLE IF NOT EXISTS rare(
  ts TEXT PRIMARY KEY, duration_s REAL, dose REAL, temperature_c REAL, charge_pct REAL);
"""

def db(path):
    c = sqlite3.connect(path); c.executescript(SCHEMA); return c

def ch_to_kev(ch, a0, a1, a2):
    return a0 + a1 * ch + a2 * ch * ch

def find_peaks(counts, a0, a1, a2, min_sigma=4.0, window=12):
    """Peaks that stand above the local continuum by min_sigma, with Poisson sigma = sqrt(background).

    Deliberately crude and deliberately explicit: a real analysis fits the peak shape. This is here to
    say 'something is at about 662 keV', not to quantify activity.
    """
    out = []
    n = len(counts)
    for i in range(window, n - window):
        left = counts[i - window:i - window // 2]
        right = counts[i + window // 2:i + window]
        bg = (sum(left) + sum(right)) / max(1, len(left) + len(right))
        if bg <= 0:
            continue
        excess = counts[i] - bg
        sigma = math.sqrt(bg)
        if excess < min_sigma * sigma:
            continue
        if counts[i] < max(counts[i - 3:i + 4]):       # local maximum only
            continue
        out.append({"channel": i, "kev": round(ch_to_kev(i, a0, a1, a2), 1),
                    "counts": counts[i], "background": round(bg, 1),
                    "sigma": round(excess / sigma, 1)})
    return out

def identify(peaks, tol_kev=12.0):
    for p in peaks:
        hits = [(abs(p["kev"] - e), nuc, note, e) for e, nuc, note in LINES if abs(p["kev"] - e) <= tol_kev]
        hits.sort()
        p["candidates"] = [{"nuclide": n, "line_kev": e, "note": note, "off_by_kev": round(d, 1)}
                           for d, n, note, e in hits]
    return peaks

def cmd_log(a):
    from radiacode import RadiaCode
    conn = db(a.db)
    rc = RadiaCode(bluetooth_mac=a.bt, serial_number=a.serial)
    serial = rc.serial_number()
    print(f"connected: {serial} fw {rc.fw_version()}", file=sys.stderr)
    client = None
    if a.mqtt:
        client = mqtt_connect(a.mqtt_host, a.mqtt_port, serial)
        mqtt_announce(client, serial)
        print(f"mqtt: announced {len(SENSORS)} sensors to {a.mqtt_host}:{a.mqtt_port}", file=sys.stderr)
    last_spec = 0.0
    while True:
        for rec in rc.data_buf():
            kind = type(rec).__name__
            if kind == "RareData":
                # Battery, temperature and the accumulated dose arrive here, every minute or so.
                # Stored raw; `status` converts the dose with TO_MICRO.
                conn.execute("INSERT OR REPLACE INTO rare VALUES(?,?,?,?,?)",
                             (rec.dt.isoformat(), rec.duration, rec.dose,
                              rec.temperature, rec.charge_level))
                continue
            if kind != "RealTimeData":
                continue
            conn.execute("INSERT OR REPLACE INTO reading VALUES(?,?,?,?,?,?)",
                         (rec.dt.isoformat(), rec.count_rate, rec.count_rate_err,
                          rec.dose_rate, rec.dose_rate_err, rec.flags))
            if client:
                client.publish(state_topic(serial), json.dumps({
                    "dose_rate": round(rec.dose_rate * TO_MICRO, 4),  # device units -> uSv/h, see TO_MICRO
                    "count_rate": round(rec.count_rate, 3),
                    "dose_rate_err": round(rec.dose_rate_err, 1),
                    "ts": rec.dt.isoformat()}))
        if time.time() - last_spec >= a.spectrum_every:
            s = rc.spectrum_accum()
            conn.execute("INSERT OR REPLACE INTO spectrum VALUES(?,?,?,?,?,?)",
                         (datetime.now(timezone.utc).isoformat(), s.duration.total_seconds(),
                          s.a0, s.a1, s.a2, json.dumps(s.counts)))
            last_spec = time.time()
        conn.commit()
        time.sleep(a.interval)

def cmd_analyse(a):
    conn = db(a.db)
    q = "SELECT ts,duration_s,a0,a1,a2,counts FROM spectrum"
    row = conn.execute(q + (" WHERE ts<=? ORDER BY ts DESC LIMIT 1" if a.at else " ORDER BY ts DESC LIMIT 1"),
                       (a.at,) if a.at else ()).fetchone()
    if not row:
        print("no spectrum stored yet", file=sys.stderr); return 2
    ts, dur, a0, a1, a2, counts = row
    counts = json.loads(counts)
    peaks = identify(find_peaks(counts, a0, a1, a2))
    print(json.dumps({"ts": ts, "duration_s": dur, "channels": len(counts),
                      "total_counts": sum(counts), "peaks": peaks}, indent=1))
    return 0

def rate_sigma(readings):
    """Standard error of the MEAN rate over `readings`, each (rate_cps, err_percent, seconds).

    The device reports its own relative uncertainty: radiacode decodes count_rate_err as an
    unsigned short divided by 10, i.e. a PERCENTAGE to one decimal. Verified in the decoder,
    not assumed. So sigma_i = rate_i * err_i / 100 and the mean of n independent readings has
    variance (1/n^2) * sum(sigma_i^2).

    When the device reports no error (zero or missing), fall back to counting statistics proper:
    Var(rate) = lambda / T where T is the EXPOSURE in seconds, not the number of samples. An
    earlier version of this used sqrt(mean/n), which silently assumed every reading was exactly
    one second of independent counting (codexmb, 4 Oct).
    """
    n = len(readings)
    if n == 0:
        return 0.0, 0.0
    mean = sum(r for r, _, _ in readings) / n
    var = 0.0
    for rate, err_pct, secs in readings:
        if err_pct and err_pct > 0:
            s_i = rate * err_pct / 100.0
        elif secs and secs > 0:
            s_i = math.sqrt(max(rate, 0.0) / secs)      # Var(rate) = lambda / T
        else:
            return mean, float('nan')                    # cannot state an uncertainty: say so
        var += s_i * s_i
    return mean, math.sqrt(var) / n


def difference_z(window, baseline):
    """Sigma of (window mean - baseline mean), carrying BOTH uncertainties.

    The baseline is estimated, not known, so its own error belongs in the denominator. Treating
    it as exact overstates the significance of every alert.
    """
    w_mean, w_sig = rate_sigma(window)
    b_mean, b_sig = rate_sigma(baseline)
    if math.isnan(w_sig) or math.isnan(b_sig):
        return float('nan'), w_mean, b_mean
    denom = math.sqrt(w_sig * w_sig + b_sig * b_sig)
    if denom <= 0:
        return 0.0, w_mean, b_mean
    return (w_mean - b_mean) / denom, w_mean, b_mean


def cmd_watch(a):
    """Rolling-baseline alerting on count rate. Prints one JSON line.

    THE THRESHOLD IS NOT CALIBRATED. 5 sigma is a placeholder until it has been checked against
    this device's real quiet-background behaviour over hours. The arithmetic below is right; what
    a real alarm should be set to is an empirical question about one detector in one place, and
    synthetic data cannot answer it.
    """
    out, why = watch_state(db(a.db), a.baseline, a.window, a.sigma)
    if out is None:
        print(why, file=sys.stderr)
        return 2
    print(json.dumps(out))
    return 0


def watch_state(conn, baseline, window, sigma):
    """The watch verdict as (dict, None), or (None, reason) when there are too few readings.
    Shared by `watch` and `status` so a dashboard shows exactly the number `watch` would print."""
    rows = conn.execute(
        "SELECT ts,count_rate,count_rate_err FROM reading ORDER BY ts DESC LIMIT ?",
        (baseline + window,)
    ).fetchall()
    need = baseline + window
    if len(rows) < need:
        return None, f"not enough readings yet: {len(rows)} of {need}"
    rows.reverse()

    # exposure per reading, from the timestamps rather than assumed
    def secs(i):
        if i == 0:
            return None
        try:
            a_ = datetime.fromisoformat(rows[i - 1][0]); b_ = datetime.fromisoformat(rows[i][0])
            d = (b_ - a_).total_seconds()
            return d if d > 0 else None
        except Exception:
            return None

    series = [(rows[i][1], rows[i][2], secs(i)) for i in range(len(rows))]
    base, win = series[:baseline], series[baseline:]
    z, w_mean, b_mean = difference_z(win, base)
    out = {"ts": rows[-1][0], "baseline_cps": round(b_mean, 3), "window_cps": round(w_mean, 3),
           "sigma": None if math.isnan(z) else round(z, 2),
           "alert": (not math.isnan(z)) and z >= sigma,
           "threshold_calibrated": False}
    if math.isnan(z):
        out["note"] = "no device error and no usable timestamps: uncertainty unknown, no alert claimed"
    return out, None


def age_s(ts, now=None):
    """Seconds since an ISO timestamp from this database, or None if it does not parse.

    radiacode stamps readings with NAIVE local time (base_time = datetime.now() at connect), so a
    naive stamp is compared with naive local now. An aware stamp is compared with aware now."""
    try:
        t = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if now is None:
        now = datetime.now(timezone.utc) if t.tzinfo else datetime.now()
    elif t.tzinfo is None and now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    return round((now - t).total_seconds(), 1)


def status(path, baseline=600, window=30, sigma=5.0, now=None):
    """Everything a dashboard needs, as one dict, read from the database WITHOUT writing to it.

    The database is opened read-only: `db()` would create an empty file at a mistyped path and the
    dashboard would then report "no readings yet" forever instead of "wrong path". A missing file is
    an error with the path in it, an empty database is ok with reading None. Those two must never
    look alike to whoever reads this."""
    if not os.path.isfile(path):
        return {"ok": False, "error": f"no radwatch database at {path}"}
    try:
        conn = sqlite3.connect("file:" + urllib.request.pathname2url(os.path.abspath(path)) + "?mode=ro",
                               uri=True)
        n = conn.execute("SELECT COUNT(*) FROM reading").fetchone()[0]
        row = conn.execute("SELECT ts,count_rate,count_rate_err,dose_rate,dose_rate_err "
                           "FROM reading ORDER BY ts DESC LIMIT 1").fetchone()
        try:
            rare = conn.execute("SELECT ts,duration_s,dose,temperature_c,charge_pct "
                                "FROM rare ORDER BY ts DESC LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            rare = None       # a database written before the rare table existed
        watch, why = watch_state(conn, baseline, window, sigma)
    except sqlite3.Error as e:
        return {"ok": False, "error": f"cannot read {path}: {e}"}
    out = {"ok": True, "readings": n, "reading": None, "device": None,
           "watch": watch, "watch_note": why}
    if row:
        ts, cps, cps_err, dr, dr_err = row
        out["reading"] = {"ts": ts, "age_s": age_s(ts, now),
                          "dose_rate_usv_h": None if dr is None else round(dr * TO_MICRO, 4),
                          "dose_rate_err_pct": dr_err,
                          "count_rate_cps": None if cps is None else round(cps, 3),
                          "count_rate_err_pct": cps_err}
    if rare:
        ts, dur, dose, temp, charge = rare
        out["device"] = {"ts": ts, "age_s": age_s(ts, now),
                         "accumulated_dose_usv": None if dose is None else round(dose * TO_MICRO, 4),
                         "accumulated_over_s": dur, "temperature_c": temp, "battery_pct": charge}
    return out


def cmd_status(a):
    """One JSON object on stdout, exit 0 when the database could be read, 2 when it could not."""
    out = status(a.db, a.baseline, a.window, a.sigma)
    print(json.dumps(out))
    return 0 if out["ok"] else 2


EXPLAIN_SYSTEM = (
    "You are writing one short paragraph for a radiation monitoring log, for a non-specialist who "
    "will read it on a phone. You are given numbers that have already been computed. Do NOT decide "
    "whether anything is anomalous: the sigma value given to you is the decision and it was made by "
    "counting statistics, not by you. Explain what the numbers mean in plain language, name the "
    "likely isotopes if peaks are listed, and say plainly if nothing of note happened. Never invent a "
    "number that is not given to you. No more than four sentences."
)


def cmd_explain(a):
    """The local model reads what the arithmetic produced and writes the sentence.

    This is the only place a model appears in radwatch, and it is downstream of every decision.
    It is handed the computed statistics and the identified peaks; it is not handed raw readings
    to judge, and it cannot raise or clear an alert.
    """
    conn = db(a.db)
    rows = conn.execute("SELECT ts,count_rate,count_rate_err FROM reading ORDER BY ts DESC LIMIT ?",
                        (a.baseline + a.window,)).fetchall()
    facts = {"readings_available": len(rows)}
    if len(rows) >= a.baseline + a.window:
        rows.reverse()
        series = [(r[1], r[2], None) for r in rows]
        z, w_mean, b_mean = difference_z(series[a.baseline:], series[:a.baseline])
        facts.update({"baseline_cps": round(b_mean, 3), "recent_cps": round(w_mean, 3),
                      "sigma_above_baseline": None if math.isnan(z) else round(z, 2),
                      "alert_threshold_sigma": a.sigma,
                      "alert_raised": (not math.isnan(z)) and z >= a.sigma,
                      "threshold_calibrated": False})
    sp = conn.execute("SELECT ts,a0,a1,a2,counts FROM spectrum ORDER BY ts DESC LIMIT 1").fetchone()
    if sp:
        peaks = identify(find_peaks(json.loads(sp[4]), sp[1], sp[2], sp[3]))
        facts["spectrum_ts"] = sp[0]
        facts["peaks"] = [{"kev": p["kev"], "sigma": p["sigma"],
                           "candidates": [c["nuclide"] for c in p["candidates"]]} for p in peaks[:8]]
    if a.facts_only:
        print(json.dumps(facts, indent=1)); return 0

    body = {"model": a.model, "temperature": 0.2, "max_tokens": 220,
            "messages": [{"role": "system", "content": EXPLAIN_SYSTEM},
                         {"role": "user", "content": json.dumps(facts, indent=1)}]}
    req = urllib.request.Request(a.endpoint.rstrip("/") + "/v1/chat/completions",
                                 json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=a.timeout) as r:
            d = json.load(r)
        print(d["choices"][0]["message"]["content"].strip())
    except Exception as e:
        # Say the model was unreachable AND print the facts, so a dead endpoint never costs you
        # the measurement. The numbers are the product; the sentence is the convenience.
        print(f"local model unreachable ({type(e).__name__}): {e}", file=sys.stderr)
        print(json.dumps(facts, indent=1))
        return 1
    return 0


def cmd_selftest(a):
    """A synthetic spectrum with two known lines, and a flat one that must yield nothing."""
    import random
    random.seed(7)
    a0, a1, a2 = 0.0, 2.4, 0.0           # 2.4 keV per channel, 1024 channels -> ~2.5 MeV
    n = 1024
    def peak_at(counts, kev, area, width_ch=4):
        c = int((kev - a0) / a1)
        for d in range(-3 * width_ch, 3 * width_ch + 1):
            if 0 <= c + d < n:
                counts[c + d] += int(area * math.exp(-0.5 * (d / width_ch) ** 2))
    flat = [max(0, int(random.gauss(300, 17))) for _ in range(n)]
    spiked = list(flat)
    peak_at(spiked, 661.657, 2600)       # Cs-137
    peak_at(spiked, 1460.8, 1800)        # K-40

    found = identify(find_peaks(spiked, a0, a1, a2))
    names = {c["nuclide"] for p in found for c in p["candidates"]}
    print(f"spiked spectrum: {len(found)} peaks, candidates {sorted(names)}")
    ok = "Cs-137" in names and "K-40" in names

    # NEGATIVE CONTROL: the same statistics with no peaks must find nothing. A finder that cannot
    # fail is not a finder.
    noise = identify(find_peaks(flat, a0, a1, a2))
    print(f"flat spectrum  : {len(noise)} peaks (must be 0)")
    ok = ok and len(noise) == 0

    # the discovery payloads must be valid JSON and name a state topic HA can read
    class _Fake:
        def __init__(self): self.msgs = []
        def publish(self, topic, payload, retain=False): self.msgs.append((topic, payload))
    f = _Fake(); mqtt_announce(f, "TEST123")
    cfgs = [json.loads(p) for t, p in f.msgs if t.endswith("/config")]
    g = _Fake(); mqtt_announce(g, "OTHER456")
    cfgs2 = [json.loads(p) for t, p in g.msgs if t.endswith("/config")]
    disc_ok = (len(cfgs) == len(SENSORS)
               and all(c["state_topic"] == state_topic("TEST123") and c["unique_id"] and c["device"] for c in cfgs)
               # two devices must never share a state topic: three boxes publishing to one topic
               # would silently overwrite each other
               and cfgs[0]["state_topic"] != cfgs2[0]["state_topic"])
    print(f"ha discovery  : {len(cfgs)} sensor configs, well formed and per-device topics: {disc_ok}")
    ok = ok and disc_ok

    # The alerting maths, with a negative control: steady Poisson counts must NOT alert.
    # Exercises the ARITHMETIC only. It cannot calibrate a device threshold: these are synthetic
    # one-second exposures with no detector behind them, which is why `watch` reports
    # threshold_calibrated: false until the real thing has run quiet for hours.
    quiet = [(random.gauss(10, math.sqrt(10)), 0.0, 1.0) for _ in range(630)]
    z_quiet, _, _ = difference_z(quiet[600:], quiet[:600])
    rise = [(random.gauss(10, math.sqrt(10)), 0.0, 1.0) for _ in range(600)] \
         + [(random.gauss(14, math.sqrt(14)), 0.0, 1.0) for _ in range(30)]
    z_loud, _, _ = difference_z(rise[600:], rise[:600])
    # and the device-reported-error path, which is what will actually run
    dev = [(10.0, 3.0, 1.0) for _ in range(600)] + [(14.0, 3.0, 1.0) for _ in range(30)]
    z_dev, _, _ = difference_z(dev[600:], dev[:600])
    print(f"stats (synthetic, NOT a calibration): quiet {z_quiet:+.1f} (<5), 40% rise {z_loud:+.1f} (>=5), "
          f"device-err path {z_dev:+.1f} (>=5)")
    alert_ok = z_quiet < 5.0 and z_loud >= 5.0 and z_dev >= 5.0
    ok = ok and alert_ok

    # status: what a dashboard reads. Four databases, four answers that must not look alike.
    import tempfile
    tmp = tempfile.mkdtemp(prefix="radwatch-selftest-")
    missing = os.path.join(tmp, "typo.sqlite")
    st_missing = status(missing)
    # NEGATIVE CONTROL: a wrong path is an error, and reading it must not create the file
    miss_ok = st_missing["ok"] is False and "no radwatch database" in st_missing["error"] \
        and not os.path.exists(missing)
    empty = os.path.join(tmp, "empty.sqlite"); db(empty).close()
    st_empty = status(empty)
    empty_ok = st_empty["ok"] is True and st_empty["reading"] is None and st_empty["readings"] == 0
    old = os.path.join(tmp, "old.sqlite")     # written before the rare table existed
    c = sqlite3.connect(old)
    c.execute("CREATE TABLE reading(ts TEXT PRIMARY KEY, count_rate REAL, count_rate_err REAL, "
              "dose_rate REAL, dose_rate_err REAL, flags INT)")
    c.execute("INSERT INTO reading VALUES('2026-10-01T12:00:00',5.0,3.0,1.2e-7,10.0,0)")
    c.commit(); c.close()
    st_old = status(old, now=datetime(2026, 10, 1, 12, 0, 30))
    old_ok = st_old["ok"] and st_old["device"] is None and st_old["reading"]["age_s"] == 30.0
    full = os.path.join(tmp, "full.sqlite"); c = db(full)
    t0 = datetime(2026, 10, 1, 12, 0, 0)
    from datetime import timedelta
    for i in range(40):
        c.execute("INSERT INTO reading VALUES(?,?,?,?,?,?)",
                  ((t0 + timedelta(seconds=i)).isoformat(), 10.0, 3.0, 1.0e-7, 10.0, 0))
    c.execute("INSERT INTO rare VALUES(?,?,?,?,?)", (t0.isoformat(), 3600, 2.5e-6, 24.5, 81.0))
    c.commit(); c.close()
    st_full = status(full, baseline=30, window=10, now=t0 + timedelta(seconds=99))
    r, d = st_full["reading"] or {}, st_full["device"] or {}
    full_ok = (st_full["ok"] and r.get("age_s") == 60.0 and r.get("dose_rate_usv_h") == 0.1
               and r.get("count_rate_cps") == 10.0 and d.get("accumulated_dose_usv") == 2.5
               and d.get("battery_pct") == 81.0 and d.get("temperature_c") == 24.5
               and st_full["watch"] is not None and st_full["watch"]["alert"] is False)
    # too few readings for the default baseline: no verdict, and a reason instead of a fake one
    st_short = status(full, now=t0)
    short_ok = st_short["watch"] is None and "not enough readings" in (st_short["watch_note"] or "")
    stat_ok = miss_ok and empty_ok and old_ok and full_ok and short_ok
    print(f"status        : missing->error {miss_ok}, empty->no reading {empty_ok}, "
          f"pre-rare db {old_ok}, full {full_ok}, short history {short_ok}")
    ok = ok and stat_ok
    import shutil; shutil.rmtree(tmp, ignore_errors=True)

    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("log"); lg.set_defaults(fn=cmd_log)
    lg.add_argument("--serial"); lg.add_argument("--bt")
    lg.add_argument("--db", default="radwatch.sqlite")
    lg.add_argument("--interval", type=float, default=2.0)
    lg.add_argument("--spectrum-every", dest="spectrum_every", type=float, default=600.0)
    lg.add_argument("--mqtt", action="store_true", help="publish to MQTT with Home Assistant discovery")
    lg.add_argument("--mqtt-host", dest="mqtt_host", default="127.0.0.1")
    lg.add_argument("--mqtt-port", dest="mqtt_port", type=int, default=1883)
    an = sub.add_parser("analyse"); an.set_defaults(fn=cmd_analyse)
    an.add_argument("--db", default="radwatch.sqlite"); an.add_argument("--at")
    w = sub.add_parser("watch"); w.set_defaults(fn=cmd_watch)
    w.add_argument("--db", default="radwatch.sqlite")
    w.add_argument("--baseline", type=int, default=600, help="readings forming the baseline")
    w.add_argument("--window", type=int, default=30, help="recent readings tested against it")
    w.add_argument("--sigma", type=float, default=5.0, help="alert threshold in sigma")
    ss = sub.add_parser("status"); ss.set_defaults(fn=cmd_status)
    ss.add_argument("--db", default="radwatch.sqlite")
    ss.add_argument("--baseline", type=int, default=600)
    ss.add_argument("--window", type=int, default=30)
    ss.add_argument("--sigma", type=float, default=5.0)
    ex = sub.add_parser("explain"); ex.set_defaults(fn=cmd_explain)
    ex.add_argument("--db", default="radwatch.sqlite")
    ex.add_argument("--endpoint", default="http://127.0.0.1:8081", help="OpenAI-compatible local model")
    ex.add_argument("--model", default="local")
    ex.add_argument("--baseline", type=int, default=600)
    ex.add_argument("--window", type=int, default=30)
    ex.add_argument("--sigma", type=float, default=5.0)
    ex.add_argument("--timeout", type=float, default=120)
    ex.add_argument("--facts-only", dest="facts_only", action="store_true",
                    help="print the facts that would be sent, and send nothing")
    st = sub.add_parser("selftest"); st.set_defaults(fn=cmd_selftest)
    a = p.parse_args()
    return a.fn(a)

if __name__ == "__main__":
    sys.exit(main() or 0)
