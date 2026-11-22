#!/usr/bin/env python3
"""Dell D6000 dock firmware update on macOS, replicating Dell A04 (D6000Updater.exe).

Steps (run one at a time, check `status` after each):
  fetch             download Dell's A04 updater, check SHA-256, unpack the .dfu files (needs 7zz)
  status            versions + DFU devices (read-only)
  extract           write st.bin / ti.bin / pr.bin from the A04 .dfu files (read-only)
  detach            HID detach: controller 06c4:c411 -> DFU (06c4:6001 app-DFU or 06c4:c302 bootloader)
  leave PID         DfuSe leave on 6001/c302 (6001 -> c302 per the updater's state machine)
  flash st|ti|pr    write + read back + compare (st only on c302, ti/pr only on 6001)
Hubs (GL) are not handled: Genesys ISP tool, Windows only; A04 hub ROMs are GL 8816/8817.
After st+ti: unplug the dock from AC and USB-C (TI/PR load their flash only at power-up).
"""
import hashlib, os, struct, subprocess, sys, tempfile
import hid, usb.core, usb.util

HERE = os.path.dirname(os.path.abspath(__file__))
DFU_DIR = os.path.join(HERE, "dfu")
VID, APP, APPDFU, BOOT = 0x06C4, 0xC411, 0x6001, 0xC302
# part -> (dfu file, A04 version, PID it must be written on, alt, address, slice of the element)
PARTS = {
    # first 0x5000 = BizLink bootloader (read-only pages, identical in all A04 ST files): skip it
    "st": ("06C4C411ST0020.dfu", "0020", BOOT, 0, 0x08005000, slice(0x5000, None)),
    "ti": ("06C4C411TI01020202.dfu", "01020202", APPDFU, 1, 0x09000000, slice(None)),
    "pr": ("06C4C411PR0B81.dfu", "0B81", APPDFU, 2, 0x00000000, slice(None)),
}


A04_URL = "https://dl.dell.com/FOLDER07605220M/1/Dock_D6000_FW_Updater_A04_F1TXX.exe"
A04_SHA256 = "3bd53ba34764ce028a008e002f6cca145928589c255c2732651759d1edcc50ab"


def fetch():
    # Dell's firmware is not redistributable: always download the original package
    with tempfile.TemporaryDirectory() as tmp:
        exe = os.path.join(tmp, "A04.exe")
        run(["curl", "-fL", "-A", "Mozilla/5.0", "-o", exe, A04_URL])
        digest = hashlib.sha256(open(exe, "rb").read()).hexdigest()
        if digest != A04_SHA256:
            sys.exit("SHA-256 mismatch: %s" % digest)
        names = [PARTS[p][0] for p in PARTS]
        run(["7zz", "e", "-y", "-o" + DFU_DIR, exe] + ["dfu/" + n for n in names])
    print("A04 firmware in", DFU_DIR)


def element(fname):
    d = open(os.path.join(DFU_DIR, fname), "rb").read()
    assert d[:5] == b"DfuSe" and d[10] == 1, fname
    o = 11 + 274  # prefix + target prefix
    nel = struct.unpack("<I", d[o - 4:o])[0]
    assert nel == 1, fname
    addr, size = struct.unpack("<II", d[o:o + 8])
    return addr, d[o + 8:o + 8 + size]


def bin_path(part):
    return os.path.join(HERE, part + ".bin")


def extract():
    for part, (f, _, _, _, addr, sl) in PARTS.items():
        eaddr, data = element(f)
        data = data[sl]
        # the write address must equal the element address plus the skipped bootloader
        assert eaddr + (sl.start or 0) == addr, (part, hex(eaddr))
        open(bin_path(part), "wb").write(data)
        print("%s.bin %d bytes @ %#x" % (part, len(data), addr))


def versions():
    d = usb.core.find(idVendor=VID, idProduct=APP)
    if d is None:
        return None
    return {"st": "%04X" % d.bcdDevice, "ti": usb.util.get_string(d, 6), "pr": usb.util.get_string(d, 7)}


def dfu_pids():
    return [d.idProduct for d in usb.core.find(find_all=True, idVendor=VID) if d.idProduct in (APPDFU, BOOT)]


def run(cmd):
    print("+", " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode:
        sys.exit("FAILED rc=%d. Do NOT unplug the dock; run `status` and retry the step." % r.returncode)


def status():
    v = versions()
    if v:
        for p, (_, want, *_rest) in PARTS.items():
            print("%-3s current %-9s A04 %-9s %s" % (p, v[p], want, "ok" if v[p] == want else "UPDATE"))
    else:
        print("controller 06c4:c411 not present")
    print("DFU devices:", ["06c4:%04x" % p for p in dfu_pids()] or "none")
    subprocess.run("dfu-util -l 2>/dev/null | grep '^Found DFU'", shell=True)


def detach():
    h = hid.device()
    h.open(VID, APP)
    try:
        # feature report ID 1, usage 0x55 = DfuSe HID detach (descriptor 0600ff..8501 0955..b182)
        h.send_feature_report([0x01, 0x55])
    finally:
        h.close()
    print("detach sent; wait a few seconds, then `status`")


def leave(pid):
    assert pid in (APPDFU, BOOT), "leave only from 6001 or c302"
    run(["dfu-util", "-d", "06c4:%04x" % pid, "-a", "0", "-s", "0x08000000:leave"])


def flash(part):
    if part == "pr":
        # dfu-util needs :force here (layout "0*001Ka" misparsed); even then the controller ACKs but keeps 0B5D (2026-11-22)
        sys.exit("Parade write is not solved yet, see README, section Parade converter")
    f, want, pid, alt, addr, _ = PARTS[part]
    path = bin_path(part)
    assert os.path.isfile(path), "run extract first"
    # writing alt0 from the app-DFU (6001) would overwrite the running app: refuse
    if dfu_pids() != [pid]:
        sys.exit("%s must be written on 06c4:%04x, present: %s" % (part, pid, ["%04x" % p for p in dfu_pids()]))
    dev = "06c4:%04x" % pid
    size = os.path.getsize(path)
    run(["dfu-util", "-d", dev, "-a", str(alt), "-s", "%#x" % addr, "-D", path])
    rb = path + ".readback"
    if os.path.exists(rb):
        os.remove(rb)
    run(["dfu-util", "-d", dev, "-a", str(alt), "-s", "%#x:%#x" % (addr, size), "-U", rb])
    ok = open(rb, "rb").read() == open(path, "rb").read()
    print("VERIFY", "OK" if ok else "MISMATCH")
    if not ok:
        sys.exit(1)
    if part == "st":
        # the bootloader must be untouched, or the next power cycle bricks the dock
        bl = os.path.join(HERE, "bootloader.readback")
        if os.path.exists(bl):
            os.remove(bl)
        run(["dfu-util", "-d", dev, "-a", "0", "-s", "0x08000000:0x5000", "-U", bl])
        same = open(bl, "rb").read() == element(f)[1][:0x5000]
        print("BOOTLOADER", "intact" if same else "CHANGED")
        if not same:
            sys.exit(1)


def main():
    a = sys.argv[1:] or ["status"]
    if a[0] == "status":
        status()
    elif a[0] == "fetch":
        fetch()
    elif a[0] == "extract":
        extract()
    elif a[0] == "detach":
        detach()
    elif a[0] == "leave" and len(a) == 2:
        leave(int(a[1], 16))
    elif a[0] == "flash" and len(a) == 2 and a[1] in PARTS:
        flash(a[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
