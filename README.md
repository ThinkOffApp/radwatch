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

What was missing, across 25 public repositories, was anything that keeps a defensible statistical
history, and anything that puts a local language model next to the detector. That is what this is.

## Commands

    radwatch.py log      poll the device into SQLite, store a spectrum periodically
    radwatch.py analyse  peak-find a stored spectrum, match gamma lines, name candidate isotopes
    radwatch.py watch    rolling-baseline significance on count rate
    radwatch.py explain  the local model reads what watch and analyse computed and writes a paragraph
    radwatch.py selftest runs with no hardware attached

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
