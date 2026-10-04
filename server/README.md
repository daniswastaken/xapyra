# Xapyra retrieval server

PC-side companion to the ESP32 firmware. The device publishes a raw MP3 byte
stream over TCP (it is the TCP *server*, port 8080 — see `src/main.cpp`). This
tool dials in as a client, records the audio, and hands it back over HTTP.

Python 3.9+, standard library only. No `pip install`, no virtualenv needed.

## Run

```bash
# from the repo root -- interactive shell, no flags needed
python server/main.py
```

`scan` finds the ESP32 on the hotspot, `record` walks you through output
choice and starts capturing, the bottom status row watches
device / link / rec / 1:1 live — it repaints every second, so LINK flips to
LIVE on its own once the device accepts (no Enter needed). Type `help`
inside. `q` / Ctrl+C quits anywhere.

```bash
python server/main.py --simple   # linear fallback for terminals
                                 # that ignore ANSI cursor moves
```

```bash
# same engine without the shell
python server/retrieval_server.py --device-host auto --tui \
  --outdir server/capture --file test123.mp3
```

## Architecture

Mirrors betterVpack's `refactor/modular-architecture`: REPL + UI helpers +
one module per command, pure stdlib.

```
server/
  main.py                 # entry: from src.app import start
  retrieval_server.py     # flag CLI (same flags, same HTTP behavior)
  tests/
    fake_device.py        # fake ESP32 for hardware-free tests
    test_retrieval.py     # 108 checks
  src/
    app.py                # REPL loop + COMMANDS table + dispatch
    ui.py                 # TUI: banner, scroll-region chatbox, prompts, tables,
                          #   live dashboard box, arrow-key picker
    cli.py                # argparse front end + single-loop serve path
    engine.py             # capture engine on a worker thread (REPL path)
    wizard.py             # no-args guided setup (scan -> pick -> record)
    audio/
      ring.py             # rolling in-memory window (AudioRing)
      recorder.py         # session files, --file append, no-overwrite guards
      link.py             # TCP client + first-byte 1:1 clock + gap accounting
      server.py           # stdlib HTTP: /stream /audio /recordings /api/status
    net/
      discover.py         # hotspot sweep for :8080
    commands/
      scan.py record.py stop.py status.py timings.py
      retrieve.py list.py doctor.py
```

Two front ends, one engine: the flag CLI runs the asyncio pipeline
in-process (`serve_forever`), the REPL runs the identical pipeline on an
engine thread. Behavior (bytes, files, HTTP) is identical either way.

## Tests

```bash
cd server/tests
python test_retrieval.py
```

## REPL commands

| Command | Does |
|---|---|
| `scan` | sweep the hotspot for ESP32 stream ports |
| `record` | pick device + output, start capturing |
| `stop` | stop capture, print the 1:1 report |
| `status` | link / ring / recording state box |
| `timings` | wall vs audio vs gap, with verdict |
| `retrieve [s] [file]` | save last N seconds from the ring |
| `list` | saved recordings |
| `doctor [host]` | python, HTTP port, device reachability |
| `clear`, `help`, `?`, `exit` | shell basics |

## 1:1 accuracy (wall time vs audio time)

Clock starts on the first audio byte. Audio time is `bytes / 16000`
(128 kbps; one 576-byte frame = 36 ms at 32 kHz). Three numbers, three
meanings:

- **wall** — everything since the first byte
- **connected** — time inside live sessions
- **gap** — wall with no link (dropouts; no bytes can exist for it)

`drift = audio − wall`, `in-session drift = audio − connected`. A long run
at −10% overall with in-session drift near zero is ~dropout time the server
rode through — every connected second intact. Only in-session drift far
from zero means frames actually vanished (device ring overflow under a weak
link), which no receiver code can recover.

## Notes and limits

- The firmware emits a bare MP3 elementary stream: playable and
  transcodable, but seeking re-reads from the start.
- A dropout never eats prior audio: reconnects append (`--file`) or open a
  new collision-proof session file; a server restart backs the old `--file`
  capture aside instead of truncating it.
- The server binds `0.0.0.0` with no auth by default. Anyone who can reach
  the port can pull microphone audio -- keep it on a trusted network, or
  pass `--http-host 127.0.0.1` and tunnel.
