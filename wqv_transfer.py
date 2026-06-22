"""
WQV-1 Casio wrist camera image downloader.

Protocol by Marcus Gröber: docs/Casio WQV-1 Wrist Camera protocol.html
Communicates via PL2303 USB-serial adapter + Casio IR transceiver.
"""

import struct
import time
import logging
import usb.core
import usb.util
from pathlib import Path
from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wqv")

# USB
PROLIFIC_VID = 0x067B
PL2303_PID   = 0x23A3
EP_OUT = 0x02
EP_IN  = 0x83

# Framing
BOF = 0xC0
EOF = 0xC1
ESC = 0x7D

# Image format
IMAGE_STRUCT_SIZE = 0x1C3D   # 7229 bytes per image
IMAGE_WIDTH  = 120
IMAGE_HEIGHT = 120

# Upload-from-watch GET/RET cycling sequences (Gröber §"Upload of all images")
GET_CMDS = [0x31, 0x51, 0x71, 0x91, 0xB1, 0xD1, 0xF1, 0x11]
RET_CMDS = [0x42, 0x44, 0x46, 0x48, 0x4A, 0x4C, 0x4E, 0x40]


# ---------------------------------------------------------------------------
# PL2303 setup
# ---------------------------------------------------------------------------

def open_pl2303():
    dev = usb.core.find(idVendor=PROLIFIC_VID, idProduct=PL2303_PID)
    if dev is None:
        raise RuntimeError("PL2303 adapter not found")
    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)
    dev.set_configuration()
    line_coding = struct.pack('<IBBB', 115200, 0, 0, 8)
    dev.ctrl_transfer(0x21, 0x20, 0, 0, line_coding)
    dev.ctrl_transfer(0x21, 0x22, 0x0003, 0, None)
    log.info("PL2303 opened at 115200 8N1")
    return dev


# ---------------------------------------------------------------------------
# Serial I/O — persistent buffer so no bytes are lost between frame reads
# ---------------------------------------------------------------------------

_rx_buf: bytearray = bytearray()
_last_rx_time: float = 0.0

# IrDA SIR is half-duplex. After the watch finishes transmitting, it needs
# time to switch its transceiver back to receive mode before it can hear us.
# 20ms is conservative but reliable; spec minimum is ~10ms.
MIN_TURNAROUND_S = 0.020


def _fill_buf(dev, timeout_ms: int = 100):
    global _rx_buf, _last_rx_time
    try:
        chunk = dev.read(EP_IN, 64, timeout=timeout_ms)
        log.debug("RX %d: %s", len(chunk), bytes(chunk).hex())
        _rx_buf.extend(chunk)
        _last_rx_time = time.monotonic()
    except usb.core.USBTimeoutError:
        pass


def serial_write(dev, data: bytes):
    gap = time.monotonic() - _last_rx_time
    if gap < MIN_TURNAROUND_S:
        time.sleep(MIN_TURNAROUND_S - gap)
    log.debug("TX %d: %s", len(data), data.hex())
    dev.write(EP_OUT, data, timeout=2000)


def read_frame(dev, timeout_ms: int = 5000) -> bytes:
    """Return one complete BOF…EOF frame, leaving any remainder in _rx_buf."""
    global _rx_buf
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        bof = _rx_buf.find(BOF)
        if bof == -1:
            _fill_buf(dev)
            continue
        if bof > 0:
            log.debug("discard %d pre-BOF bytes: %s", bof, bytes(_rx_buf[:bof]).hex())
            del _rx_buf[:bof]

        # Scan for unescaped EOF
        i, in_esc, found = 1, False, False
        while i < len(_rx_buf):
            b = _rx_buf[i]
            if in_esc:
                in_esc = False
            elif b == ESC:
                in_esc = True
            elif b == EOF:
                found = True
                break
            i += 1

        if not found:
            _fill_buf(dev)
            continue

        frame = bytes(_rx_buf[:i + 1])
        del _rx_buf[:i + 1]
        log.debug("FRAME %d: %s", len(frame), frame.hex())
        return frame

    raise TimeoutError("Timed out waiting for a complete frame")


# ---------------------------------------------------------------------------
# Protocol framing
# ---------------------------------------------------------------------------

def build_frame(address: int, control: int, data: bytes = b'') -> bytes:
    payload = bytes([address, control]) + data
    csum = sum(payload) & 0xFFFF
    payload += bytes([csum >> 8, csum & 0xFF])
    out = bytearray([BOF])
    for b in payload:
        if b in (BOF, EOF, ESC):
            out += bytes([ESC, b ^ 0x20])
        else:
            out.append(b)
    out.append(EOF)
    return bytes(out)


def parse_frame(raw: bytes) -> tuple[int, int, bytes]:
    if not raw or raw[0] != BOF:
        raise ValueError(f"Missing BOF: {raw[:4].hex()}")
    unesc = bytearray()
    i = 1
    while i < len(raw):
        b = raw[i]
        if b == EOF:
            break
        elif b == ESC:
            i += 1
            unesc.append(raw[i] ^ 0x20)
        else:
            unesc.append(b)
        i += 1
    if len(unesc) < 4:
        raise ValueError(f"Frame too short after unescape: {unesc.hex()}")
    addr, ctrl = unesc[0], unesc[1]
    data = bytes(unesc[2:-2])
    csum = (unesc[-2] << 8) | unesc[-1]
    expected = sum(unesc[:-2]) & 0xFFFF
    if csum != expected:
        log.warning("Checksum mismatch: got %#06x want %#06x — accepting anyway (no retry in protocol)", csum, expected)
    log.debug("PARSE addr=%02x ctrl=%02x data=%s", addr, ctrl, data.hex())
    return addr, ctrl, data


def expect(dev, ctrl_byte: int, timeout_ms: int = 5000) -> bytes:
    """Read one frame and assert the control byte matches."""
    raw = read_frame(dev, timeout_ms)
    _, ctrl, data = parse_frame(raw)
    if ctrl != ctrl_byte:
        raise ValueError(f"Expected ctrl {ctrl_byte:#04x}, got {ctrl:#04x}")
    return data


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------

def handshake(dev) -> int:
    global _rx_buf
    _rx_buf.clear()

    log.info("Sending connection request — put watch in PC/transfer mode")

    # Step 1+2: send FF B3 until watch replies FF A3 + timestamp
    timestamp = None
    for _ in range(30):
        serial_write(dev, build_frame(0xFF, 0xB3))
        try:
            raw = read_frame(dev, timeout_ms=500)
            _, ctrl, ts = parse_frame(raw)
            if ctrl == 0xA3:
                timestamp = ts
                break
        except (TimeoutError, ValueError):
            pass
    if timestamp is None:
        raise RuntimeError("Watch did not respond — is it in transfer mode?")
    log.info("Watch timestamp: %s", timestamp.hex())

    # Step 3: echo timestamp back, assign address 0x20
    addr = 0x20
    serial_write(dev, build_frame(0xFF, 0x93, timestamp + bytes([addr])))

    # Step 4: watch sends <adr> 63 (possibly repeated)
    for _ in range(10):
        raw = read_frame(dev)
        _, ctrl, _ = parse_frame(raw)
        if ctrl == 0x63:
            break
    log.info("Got address confirm (63h)")

    # Step 5: PC sends <adr> 11
    serial_write(dev, build_frame(addr, 0x11))

    # Step 6: watch sends <adr> 01 — may still be draining 63 frames, or may
    # skip 01 entirely and go silent. Either is treated as success.
    for _ in range(10):
        try:
            raw = read_frame(dev, timeout_ms=500)
            _, ctrl, _ = parse_frame(raw)
            if ctrl == 0x01:
                log.info("Got final OK (01h)")
                break
            if ctrl == 0x63:
                log.debug("Draining extra 63h")
        except TimeoutError:
            log.info("Watch silent after 11h — assuming connected")
            break

    log.info("Handshake complete, addr=%#04x", addr)
    return addr


# ---------------------------------------------------------------------------
# Image transfer  (Gröber §"Upload of all images from the watch")
# ---------------------------------------------------------------------------

def download_all(dev, addr: int, on_image=None):
    """
    Run the full upload-all-images protocol.
    Calls on_image(index, raw_bytes) as each image completes, if provided.
    Returns list of raw image structs.
    """

    # Prelude: request all images
    log.info("Requesting all images from watch")
    serial_write(dev, build_frame(addr, 0x10, b'\x01'))
    expect(dev, 0x21)

    serial_write(dev, build_frame(addr, 0x11))
    # Response: 20h  07h FAh 1Ch 3Dh <image_count>
    prelude = expect(dev, 0x20)
    log.info("Prelude data: %s", prelude.hex())
    if len(prelude) < 5 or prelude[:4] != bytes([0x07, 0xFA, 0x1C, 0x3D]):
        raise ValueError(f"Unexpected prelude: {prelude.hex()}")
    image_count = prelude[4]
    log.info("Watch has %d image(s)", image_count)

    # Confirm start
    serial_write(dev, build_frame(addr, 0x32, b'\x06'))
    expect(dev, 0x41)

    # Pump data, saving each image as soon as its bytes arrive
    total_bytes = image_count * IMAGE_STRUCT_SIZE
    buf = bytearray()
    images = []
    images_saved = 0
    get_idx = 0
    while len(buf) < total_bytes:
        get = GET_CMDS[get_idx % len(GET_CMDS)]
        expected_ret = RET_CMDS[get_idx % len(RET_CMDS)]
        serial_write(dev, build_frame(addr, get))
        raw = read_frame(dev, timeout_ms=10000)
        _, ctrl, data = parse_frame(raw)
        if ctrl != expected_ret:
            log.warning("Expected ret %02x got %02x", expected_ret, ctrl)
        payload = data[1:] if (data and data[0] == 0x05) else data
        buf.extend(payload)
        log.info("Received %d/%d bytes (packet %d, %d payload bytes)",
                 len(buf), total_bytes, get_idx, len(payload))
        get_idx += 1

        # Save any newly completed images immediately
        while images_saved < len(buf) // IMAGE_STRUCT_SIZE:
            start = images_saved * IMAGE_STRUCT_SIZE
            raw_img = bytes(buf[start:start + IMAGE_STRUCT_SIZE])
            images.append(raw_img)
            if on_image:
                on_image(images_saved, raw_img)
            images_saved += 1

    # Termination — exact response bytes vary by watch; log whatever arrives.
    serial_write(dev, build_frame(addr, 0x54, b'\x06'))
    try:
        raw = read_frame(dev, timeout_ms=3000)
        _, ctrl, _ = parse_frame(raw)
        log.info("Termination step 1: ctrl=%02x (expected 61h)", ctrl)
        serial_write(dev, build_frame(addr, 0x53))
        raw = read_frame(dev, timeout_ms=3000)
        _, ctrl, _ = parse_frame(raw)
        log.info("Termination step 2: ctrl=%02x (expected 63h)", ctrl)
    except (TimeoutError, ValueError) as e:
        log.warning("Termination incomplete (%s) — data received OK, continuing", e)

    log.info("Transfer complete")
    return images


# ---------------------------------------------------------------------------
# Image decoding
# ---------------------------------------------------------------------------

def parse_image(raw: bytes) -> dict:
    name   = raw[0:24].rstrip(b' \x00').decode('ascii', errors='replace')
    year   = raw[24] + 2000
    month  = raw[25]
    day    = raw[26]
    minute = raw[27]
    hour   = raw[28]
    pixels = []
    for b in raw[29:29 + 7200]:
        pixels.append(255 - ((b & 0x0F) * 17))
        pixels.append(255 - ((b >> 4) & 0x0F) * 17)
    return dict(name=name, year=year, month=month, day=day,
                hour=hour, minute=minute,
                pixels=pixels[:IMAGE_WIDTH * IMAGE_HEIGHT])


def save_images(img: dict, stem: Path):
    """Save as both PGM and PNG to the same stem path."""
    pixels = bytes(img['pixels'])

    with open(stem.with_suffix('.pgm'), 'wb') as f:
        f.write(f"P5\n{IMAGE_WIDTH} {IMAGE_HEIGHT}\n255\n".encode())
        f.write(pixels)

    Image.frombytes('L', (IMAGE_WIDTH, IMAGE_HEIGHT), pixels).save(
        stem.with_suffix('.png'))

    log.info("Saved %s (.pgm + .png)", stem.name)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    out_dir = Path("images")
    out_dir.mkdir(exist_ok=True)

    dev = open_pl2303()
    try:
        def save_image(index, raw_img):
            img = parse_image(raw_img)
            ts = f"{img['year']}{img['month']:02d}{img['day']:02d}_{img['hour']:02d}{img['minute']:02d}"
            name = img['name'] or f"img{index+1:03d}"
            stem = (out_dir / f"{ts}_{name}".replace('/', '-').replace(' ', '_'))
            stem.with_suffix('.raw').write_bytes(raw_img)
            save_images(img, stem)
            log.info("  #%d  %r  %d-%02d-%02d %02d:%02d",
                     index + 1, img['name'], img['year'], img['month'],
                     img['day'], img['hour'], img['minute'])

        addr = handshake(dev)
        images = download_all(dev, addr, on_image=save_image)
        log.info("Done — %d image(s) in ./%s/", len(images), out_dir)
    finally:
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
