# Retrieval server

PC-side companion to the ESP32 firmware. The device publishes a raw MP3 byte
stream over TCP (it is the TCP *server*, port 8080 — see `src/main.cpp:255`).
This script dials in as a client, keeps the audio, and hands it back over
HTTP:

- **records** every session to `recordings/xapyra-<timestamp>.mp3`
- **retains** a rolling in-memory window of the last `--retention` seconds
- **retrieves** any slice of that window on demand: `/audio?seconds=10`
- **relays** a live stream at `/stream`
- **reconnects** on its own after WiFi drops, reboots, or router resets

Python 3.9+, standard library only. No `pip install`, no virtualenv needed.

## Run

```bash
# from the repo root -- your ESP32's IP (printed on the serial monitor at boot)
python server/retrieval_server.py --device-host 192.168.137.156 \
  --outdir server/capture --file test123.mp3
```

Then open <http://localhost:8081>. The device IP is the one the ESP32 prints on
its serial monitor at boot (`WiFi CONNECTED, IP: ...`). On a Windows hotspot the
device is on `192.168.137.x`; your laptop's hotspot address is `192.168.137.1`,
so a device IP there will look like `192.168.137.156`. `192.168.1.42` in older
docs is a placeholder, not a real address — using it will time out and the green
LED will not blink.

### Options

| Flag | Env var | Default | Meaning |
|---|---|---|---|
| `--device-host` | `XAPYRA_DEVICE_HOST` | `127.0.0.1` | ESP32 IP or hostname |
| `--device-port` | `XAPYRA_DEVICE_PORT` | `8080` | ESP32 TCP port (`TCP_PORT`) |
| `--http-host` | `XAPYRA_HTTP_HOST` | `0.0.0.0` | bind address |
| `--http-port` | `XAPYRA_HTTP_PORT` | `8081` | bind port; `0` picks a free one |
| `--retention` | `XAPYRA_RETENTION` | `120` | seconds kept in memory |
| `--bitrate-kbps` | `XAPYRA_BITRATE_KBPS` | `128` | only used to report buffered seconds |
| `--outdir` | `XAPYRA_OUTDIR` | `recordings` | where recordings are written |
| `--file` | `XAPYRA_FILE` | unset | write everything to this one `.mp3` instead of one timestamped file per session |
| `--run-for` | `XAPYRA_RUN_FOR` | `0` | stop N seconds after the first audio byte (1:1 test). `0` = run until Ctrl+C |
| `--no-record` | | off | serve and retain, but write nothing to disk |
| `-v` / `-vv` / `-q` | | `-v` | request logs / debug / warnings only |

## Endpoints

| Route | Returns |
|---|---|
| `GET /` | dashboard: live player, link state, recordings, retrieval form |
| `GET /stream` | live MP3, chunked, mirrors the device; ends if the device goes quiet |
| `GET /stream?seconds=N` | the same, but stops by itself after N seconds |
| `GET /audio?seconds=N` | MP3 of the last N seconds (`0 < N <= retention`) |
| `GET /recordings` | JSON list of saved sessions |
| `GET /recordings/<name>` | one recording; supports `Range` for seeking |
| `GET /api/status` | JSON: link state, ring fill, byte counts, recordings |
| `GET /healthz` | `200` while the link is up, `503` while it is down |

Errors are plain text with a real status code: `400` bad parameter, `404`
unknown route or file, `405` non-GET, `416` unsatisfiable `Range`, `503`
device down or window empty.

## Examples

```bash
# last 15 seconds, saved to disk
curl -o last15.mp3 "http://localhost:8081/audio?seconds=15"

# play the live stream
mpv http://localhost:8081/stream
ffplay -nodisp -autoexit http://localhost:8081/stream

# grab exactly 30s of live audio and stop (a bounded, scriptable capture)
curl -o live30.mp3 "http://localhost:8081/stream?seconds=30"

# what does the link look like right now?
curl -s http://localhost:8081/api/status | python -m json.tool

# did the device stay connected?
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8081/healthz
```

## No hardware? Use the fake device

`fake_device.py` speaks the same wire format as the firmware — MPEG-1 Layer III,
128 kbps, 32 kHz, mono, 576 bytes per frame — so you can exercise the whole
server on one machine:

```bash
python server/fake_device.py --port 8080          # terminal 1
python server/retrieval_server.py --device-host 127.0.0.1   # terminal 2
```

`--speed 8` streams faster than realtime to fill the ring quickly, and
`--duration 30` closes the link after N seconds so you can watch the server
notice and reconnect.

## 1:1 accuracy (wall time vs audio time)

The server starts a clock on the first audio byte. Audio time is
`bytes / 16000` (128 kbps = 16000 B/s; one 576-byte frame = 36 ms at 32 kHz).
Every session end, shutdown, and `/api/status` → `timing` reports
wall vs audio vs drift:

```bash
python server/retrieval_server.py --device-host 192.168.137.156 \
  --outdir server/capture --file test10.mp3 --run-for 10
# 1:1 report file=test10.mp3 wall=10.09s audio=11.95s drift=+1.86s (+18.42%)
```

Two things to know when reading that report:

- **Pre-connect backlog.** The encoder runs even with no client, filling a
  32 KB ring (`src/main.cpp:329`). On connect that backlog (0–2 s of audio)
  flushes instantly, so short captures read fast — up to +20% on a 10 s run.
  It shrinks with window length and is real audio, not duplication.
- **Steady state is 1:1.** Past the flush, measured 15866 B/s vs 16000
  (−0.84%). If steady-state drift ever exceeds a few percent, the cause is
  device-side (ADC clock vs 32 kHz, or dropped frames under a weak link) —
  the server writes exactly what TCP delivers.

## Tests

```bash
python server/test_retrieval.py
```

Drives the real HTTP server against a fake device and checks the routing,
window accuracy, MP3 frame alignment, `Range` handling, path-traversal
rejection, the live stream, and — most importantly — that the reconnect loop
survives the device being killed outright. Exits non-zero on any failure.

## Notes and limits

- The firmware emits a bare MP3 elementary stream with no container, container
  metadata, or seek index. Each session file is therefore a raw `.mp3`:
  playable and transcodable, but seeking within it re-reads from the start.
  `Range` requests work because the server owns the bytes and tracks offsets.
- A retrieval window starts and ends on a TCP read boundary, so it can be off
  by up to one read (~8 ms) from the requested duration.
- If the ring has no audio that recent, `/audio` returns `503` rather than
  serving stale audio — a silent buffer and a stale buffer should not look the
  same to a caller.
- The server binds `0.0.0.0` by default with no authentication. Anyone who can
  reach the port can pull the microphone audio. Keep it on a trusted network, or
  pass `--http-host 127.0.0.1` and tunnel.
- Long-running sessions append to one file per device session with no rotation;
  wrap with `--outdir` on a sized volume, or `--no-record`.
