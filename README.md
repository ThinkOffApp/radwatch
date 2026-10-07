# radwatch

Continuous radiation logging and analysis for a RadiaCode 10x, built around one rule:

**Arithmetic decides. The model explains.**

Counting statistics are Poisson and the right anomaly test is arithmetic. A language model is
slower and worse at it, and would be unaccountable. So nothing in the measuring path asks a model
anything, and the model sits downstream of every decision, turning computed numbers into a sentence.

## What is here, and what deliberately is not

The RadiaCode ecosystem already has good software. Before writing anything we surveyed it:
[cdump/radiacode](https://github.com/cdump/radiacode) for the protocol,
[303Bryan/ha-radiacode](https://github.com/303Bryan/ha-radiacode) for Home Assistant entities,
[darkmatter2222/Open-RadiaCode-Android](https://github.com/darkmatter2222/Open-RadiaCode-Android)
for a phone app, and two projects that plot tracks on maps. We use those rather than reimplement them.

Across the 25 repositories that search returned, **this survey found** nothing that keeps a
defensible statistical history, and nothing that puts a local language model next to the detector.
That is a statement about what we reviewed on 4 Oct 2026, not a claim about the whole ecosystem.
If one of these exists and we missed it, please open an issue and we will use yours instead.

## Commands

    radwatch.py log      poll the device into SQLite, store a spectrum periodically
    radwatch.py analyse  peak-find a stored spectrum, match gamma lines, name candidate isotopes
    radwatch.py watch    rolling-baseline significance on count rate
    radwatch.py status   one JSON object for dashboards: latest reading, battery, temperature,
                         accumulated dose and the watch verdict, read from the database read-only
    radwatch.py explain  the local model reads what watch and analyse computed and writes a paragraph
    radwatch.py selftest runs with no hardware attached

`status` is for other programs (CarWatch's Radiation page reads it). It never computes anything
`watch` does not: the verdict comes from the same function. Exit 0 means the database was read,
even when it holds no readings yet (`"reading": null`). Exit 2 means it could not be read, with the
reason in `"error"`, and a mistyped `--db` is reported rather than silently created as an empty file.
Battery, temperature and accumulated dose come from the device's periodic RareData records, which
`log` now stores in a `rare` table; a database written by an older `log` reports `"device": null`.
Dose values use one `TO_MICRO` factor shared with the MQTT path, unverified against the device.

## The statistics, stated honestly

Per reading the device reports its own relative uncertainty: `radiacode` decodes `count_rate_err`
as an unsigned short divided by ten, so it is a percentage. sigma_i = rate_i * err_i / 100, and the
mean of n readings carries (1/n^2) * sum(sigma_i^2). The test statistic is the difference of the
window and baseline means over sqrt(sig_win^2 + sig_base^2): the baseline is an estimate, so its
uncertainty belongs in the denominator too. Where the device reports no error, it falls back to
Var(rate) = lambda/T with T the exposure in seconds taken from the timestamps. Where neither is
available it returns NaN and claims **no alert**, rather than inventing a denominator.

**The 5 sigma threshold is not calibrated.** `watch` emits `threshold_calibrated: false` on every
line and will keep doing so until the real detector has run quiet for hours in the place it lives.
Synthetic data cannot calibrate a device alarm.

## The model's job, and its limits

`explain` is handed the computed statistics and the identified peaks. It is not handed raw readings
to judge, it cannot raise or clear an alert, and if the endpoint is unreachable it prints the facts
and exits non-zero. The numbers are the product; the sentence is the convenience.

## Still missing

- **GPS.** Dose against route is the interesting use in a car and there is no position source on the
  Pi today: no gpsd, no serial GPS. That needs hardware before it can be built.
- **Calibration.** See above. Needs the device.
- Gamma line energies in `LINES` are the standard ones but have not each been checked against a
  published table. Verify against IAEA or LNHB before publishing any number from them.

## Credits

This stands on other people's work, and uses it rather than reimplementing it:

- **[cdump/radiacode](https://github.com/cdump/radiacode)** — the Python library that talks to the
  device over USB and Bluetooth. Everything here depends on it.
- **[303Bryan/ha-radiacode](https://github.com/303Bryan/ha-radiacode)** — Home Assistant integration
  with live entities, device controls and alarm thresholds. Use this for the dashboard.
- **[darkmatter2222/Open-RadiaCode-Android](https://github.com/darkmatter2222/Open-RadiaCode-Android)**
  — Android app with BLE monitoring, widgets and isotope identification.
- **[matveynator/chicha-isotope-map](https://github.com/matveynator/chicha-isotope-map)** and
  **[igmrlm/RadiacodeMapGenerator](https://github.com/igmrlm/RadiacodeMapGenerator)** — plotting
  tracks and heatmaps on maps.
- **[ckuethe/radiacode-tools](https://github.com/ckuethe/radiacode-tools)** — auxiliary tooling.

The RadiaCode devices are made by Scan-Electronics. This project is not affiliated with them.

## Status

Early. The device has not arrived yet, so nothing here has been exercised against real hardware:
`selftest` passes and the empty-database paths work, which is not the same thing. Treat every
number it produces as unverified until that changes.
