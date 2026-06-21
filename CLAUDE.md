# CLAUDE.md — wrist-cam-2026

## What this project is

A Python script that transfers images from a Casio WQV-1 wrist camera to macOS via the original Casio OBC-60 IR transceiver, without needing the original Windows-only WQV Link software.

## Stack

- **Python 3.13**, managed with `uv`
- **pyusb** — talks directly to the PL2303 USB-serial adapter without a kernel driver
- **libusb** (system, via Homebrew) — backend for pyusb
- Run with: `uv run python wqv_transfer.py`

## Hardware context

- The USB-to-serial adapter is a **Prolific PL2303GS** (VID `0x067B`, PID `0x23A3`)
- No kernel driver exists for this chip on macOS 26 (Tahoe). We bypass it entirely with pyusb/libusb — no `/dev/cu.*` device is needed
- The IR transceiver is connected to the adapter's serial port at **115200 baud, 8N1**
- An FTDI-based adapter would work differently (would create `/dev/cu.usbserial-*` and could use pyserial)

## Protocol

Documented by Marcus Gröber: https://www.mgroeber.de/wqvprot.html (also archived in `docs/`)

Frame format: `BOF(C0) | addr | ctrl | data | checksum(16-bit BE) | EOF(C1)`, with `7D`-escape for BOF/EOF/7D bytes.

The "upload all images" flow is:
1. Handshake (`FF B3` ↔ `FF A3+ts` ↔ `FF 93+ts+addr` ↔ `adr 63` ↔ `adr 11` ↔ `adr 01`)
2. Mode select: `adr 10 01` ↔ `adr 21` ↔ `adr 11` ↔ `adr 20 07FA1C3D <count>`
3. Start: `adr 32 06` ↔ `adr 41`
4. Data pump: `adr <get>` ↔ `adr <ret> 05 <data>` (GET: 31,51,71,91,B1,D1,F1,11 cycling; RET: 42,44,46,48,4A,4C,4E,40)
5. Terminate: `adr 54 06` ↔ `adr 61` ↔ `adr 53` ↔ `adr 63`

## Known quirks of this specific watch unit

- **No `01h` in handshake:** After we send `adr 11h`, the spec says the watch replies `adr 01h`. This unit sends extra `63h` frames then goes silent. Silence is treated as success.
- **Wrong termination bytes:** The spec says step 5 starts with `< adr 61h`. This unit replies with `43h`. The termination code logs what it receives instead of asserting exact values.
- **IrDA turnaround:** The watch needs ~20ms after transmitting before it can receive. `serial_write()` enforces this gap via `MIN_TURNAROUND_S = 0.020`. Without this, the watch shows "error" and aborts after ~2 packets.

## Image format

Each image is 7229 bytes (`0x1C3D`): 24-byte name + 5 date/time bytes + 7200 bytes of 4-bit grayscale pixels (120×120, 2 pixels/byte, high nibble first). Pixel values are **inverted** — decode as `255 - nibble * 17`.

Output is PGM (P5 binary), one file per image, saved to `images/` as each image completes during transfer.

## IO architecture

- **`_rx_buf`** — module-level bytearray; USB bulk reads arrive in 64-byte chunks that may span multiple protocol frames. All bytes are kept here between calls so nothing is lost.
- **`_last_rx_time`** — tracks last receive timestamp for turnaround enforcement.
- **`read_frame()`** — extracts one complete BOF→EOF frame from `_rx_buf`, refilling from USB as needed.
- All TX/RX at DEBUG level; progress at INFO level. Run with `logging.DEBUG` to see raw bytes.
