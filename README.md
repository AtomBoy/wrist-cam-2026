# WQV-1 Wrist Camera Transfer

Transfers images from a Casio WQV-1 wrist camera to a modern Mac, without the original Windows-only WQV Link software.

![Sample Image][sample.png]

## Hardware

- **Casio WQV-1** wrist camera (module 2220)
- **Casio OBC-60** IR transceiver (the original serial pod that shipped with the watch)
- **USB-to-serial adapter** with a Prolific PL2303GS chip (idVendor `0x067B`, idProduct `0x23A3`)

### Getting the adapter working on macOS

The PL2303 chip has no working kernel driver on macOS 15+ (Sequoia) or macOS 26 (Tahoe). The official Prolific driver packages are too old and are rejected by the OS.

**Solution:** bypass the kernel driver entirely using [libusb](https://libusb.info) via [pyusb](https://github.com/pyusb/pyusb). This talks directly to the USB device in userspace — no driver installation or SIP disabling needed.

```bash
brew install libusb
uv add pyusb
```

The adapter enumerates as:
- Bulk OUT endpoint `0x02` — write to watch
- Bulk IN endpoint `0x83` — read from watch
- Interrupt IN endpoint `0x81` — status (ignored)

If you have an **FTDI FT232RL-based** adapter instead, Apple ships a native `AppleUSBFTDI` driver and a `/dev/cu.usbserial-*` port will appear automatically — you can use `pyserial` instead of pyusb in that case.

## Protocol

The WQV-1 uses a framed binary protocol over IrDA (not standard IrLAP, but similar):

| Field | Value |
|---|---|
| Baud rate | 115,200 |
| Format | 8N1 |
| BOF | `0xC0` |
| EOF | `0xC1` |
| Escape byte | `0x7D` (followed by `byte XOR 0x20`) |
| Checksum | 16-bit sum of all bytes after BOF, before escaping, high byte first |

Full protocol documentation: **Marcus Gröber, "Casio WQV-1 Wrist Camera protocol"** https://www.mgroeber.de/wqvprot.html

### Image format

Each image is a fixed 7,229-byte (`0x1C3D`) struct:

```c
struct {
    char name[24];                  // space-padded
    uint8_t year_minus_2000;
    uint8_t month, day;
    uint8_t minute, hour;
    uint8_t pixel[120 * 120 / 2];  // 4-bit grayscale, 2 pixels per byte
};
```

Pixels are stored as 4-bit nibbles, low nibble first. Values are **inverted** relative to display brightness (0 = white, 15 = black) — the transfer script applies `255 - value * 17` when decoding. (Note: Gröber's documentation says high nibble first; this unit sends low nibble first.)

### Quirks of this specific watch

The Gröber documentation describes the reference protocol. This particular WQV-1 unit behaves slightly differently:

- **Handshake step 6:** The protocol specifies the watch should send `<adr> 01h` after receiving `<adr> 11h`. This watch sends a few extra `63h` frames and then goes **silent** instead. The handshake code treats silence (timeout after draining `63h` frames) as success.

- **Termination:** The protocol specifies `> 54h 06h / < 61h / > 53h / < 63h`. This watch responds to `54h 06h` with `43h` instead of `61h`. The termination code logs whatever it receives rather than asserting exact values.

- **IrDA turnaround:** The watch needs ~20ms after finishing a transmission before it can receive. Sending a GET command too quickly causes the watch to display "error" and abort. A 20ms minimum inter-frame gap is enforced in software.

## Running

```bash
# Install dependencies
brew install libusb
uv sync

# Put the watch into transfer mode: press the bottom-right button, select PC
uv run python wqv_transfer.py
```

Images are saved to `images/` as both `.pgm` and `.png` files, named `YYYYMMDD_HHMM_<name>`. Each image is saved as it arrives — you don't have to wait for the full transfer to complete.

Positioning: hold the watch face within ~5–10cm of the IR transceiver window, perpendicular. Even a small angle or movement mid-transfer will drop the IR link.

## References

- Marcus Gröber's protocol page (primary source, all reverse-engineering credit): https://www.mgroeber.de/wqvprot.html
- WQV Wristcam Tool (Java, Linux/WQV-2, same protocol): https://wqv-wristcam.sourceforge.net/
