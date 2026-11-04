# History

What was learned building this, on one BLUETTI Elite 10 (200 W, 128 Wh, LiFePO4) powering a Mac, from September to November
2026. The code began inside another application, and moved here in November 2026.

## Choosing a library

- **The station can't tell a computer anything by itself.** The Elite 10 has no USB-UPS signal, no NUT support, and no public
  cloud API, so Bluetooth is the only way to learn its state.
- **Options surveyed:** `bluetti-mqtt` had no confirmed Elite 10 support. BLUETTI's own `bluetti-bluetooth-lib` needs a
  per-device authorization file obtainable only through their app or partner flow. `Patrick762/bluetti-bt-lib` listed the EL10
  and needs no pairing or account, so it won. `thatguy-za/esp32-nut-bluetti` independently confirms the same protocol.
- **The Elite 10 encrypts.** The library handles this for reads, through `recognize_device`.

## Deciding that the power is out

- **Voltage, not watts.** AC input wattage can read near zero from measurement noise with mains present. Voltage collapses to
  about 0 only in a real outage, so presence means voltage above 10 V.
- **One read isn't enough.** The first version flipped on a single read, and paged false outages every few minutes overnight on
  2026-09-15, while an independent log of the input showed mains never dropped. Requiring agreeing reads helped, but a
  sustained misread could still get through. So an outage also needs the battery to have fallen across those reads, since a real
  outage drains it, and any rise restarts the window. Settled on 3 reads (about 6 minutes at the 2-minute poll) and a 2% fall: a
  few minutes' delay is cheap next to another false alarm.

## Reading reliably

- **One connection at a time.** On 2026-09-15 about 40% of reads failed with a diagnostic logger running beside the monitor.
  Both opened a connection per read, and the device supports one, so they starved each other. A phone with the vendor's app open
  does the same.
- **The library's 5-second scan is too short.** On the night of 2026-10-10 about 100 reads failed, mostly "Could not recognize",
  and each read made up to five connections, each after a 5-second scan. Now the device is recognized once and remembered, each
  read scans for 20 seconds and makes one connection, a failed read is retried once, and the error carries the library's reason.
  The next night: 428 reads, none failed, the longest gap 3.3 minutes.

## ECO mode

- **Why it matters.** AC ECO switches the AC output off after a while at low load. A computer idling overnight can be below the
  threshold, so the station takes it down even though wall power never failed. The manual says ECO is unavailable while charging;
  we chose not to rely on that, since the cost of being wrong is the machine going dark.
- **The write.** `bluetti-bt-lib`'s writer refuses encrypted models, but its reader already does the key exchange and encrypts what
  it sends. A small subclass writes through it. It works on the Elite 10: both writes echoed exactly, and a switch turned on in
  the app was switched off and read back off, on the same connection and on a fresh one (2026-10-12). `bluetti-bt-lib` issue #84
  reports the same approach on a Handsfree 2.
- **The one unexplained failure.** The first attempt, with both switches already off, never finished the key exchange in 30 seconds
  and wrote nothing. A read-only probe (`scripts/bluetti_handshake_probe.py`) then ran 10 key exchanges, 8 back to back with gaps of
  0-3 seconds, all normal in about 1.4 seconds, none restarted inside the encrypted channel. A second client couldn't even find the
  device while one was connected. Most likely cause: the monitor connected between the script's scan and its connect, macOS shares
  one link among apps, and the script joined that session and missed the challenge. A stalled key exchange is now retried on a new
  connection, with nothing written before it finishes.

## The time-left estimate

- **The station's own estimate** is computed from the output watts, so it jumps with each spike (196-301 minutes within minutes), and
  it leaves out the inverter's own draw, reading about 1.5 times too long. On wall power the same field is the time to full.
- **The inverter draws about 15 W itself.** In a 20-minute unplug test on 2026-10-12, about 35 W left the battery while the output
  read 12-39 W.
- **Early runway figures were wrong.** A first short test extrapolated to about 100 minutes; a Mac with its display off in fact ran
  about 4.5 hours.
- **A drain to the cut gives the truth at every reading,** so candidates can be scored fairly ([drills.md](drills.md)). Every
  percentage-based estimate read long near the end, because the percentage lags the energy and falls fast near empty. Counting energy
  from watts, which never looks at the percentage again, beat them all.

## A status file, not an API

The monitor started inside the program that needed its answers, which tied that program to a Terminal window, since macOS only lets
a process started there use Bluetooth. Splitting the monitor into its own process with a status file fixed that: readers need no
Bluetooth, and can be restarted from anywhere. `checked_at` is rewritten on every attempt, so a reader can tell a dead monitor from a
quiet one.
