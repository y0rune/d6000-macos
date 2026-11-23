#!/usr/bin/env python3
"""Dell D6000 dock firmware update on macOS, replicating Dell A04 (D6000Updater.exe).

Whole update in one go (asks before each write):
  upgrade           status -> controller (c302) -> TI + Parade (6001) -> back to normal
Single steps (check `status` after each):
  fetch             download Dell's A04 updater, check SHA-256, unpack the .dfu files (needs 7zz)
  status            versions + DFU devices (read-only)
  extract           write st.bin / ti.bin / pr.bin from the A04 .dfu files (read-only)
  detach            HID detach: controller 06c4:c411 -> DFU (06c4:6001 app-DFU or 06c4:c302 bootloader)
  leave PID         DfuSe leave on 6001/c302 (6001 -> c302 per the updater's state machine)
  flash st|ti|pr    write (st only on c302, ti/pr only on 6001); ti/pr are read back and compared,
                    st is proven by the version after `leave c302` (the bootloader cannot upload)
Hubs (GL) are not handled: Genesys ISP tool, Windows only; A04 hub ROMs are GL 8816/8817.
After st+ti: unplug the dock from AC and USB-C (TI/PR load their flash only at power-up).
"""
import hashlib, os, struct, subprocess, sys, tempfile, time
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
    other_chips()
    print("DFU devices:", ["06c4:%04x" % p for p in dfu_pids()] or "none")
    subprocess.run("dfu-util -l 2>/dev/null | grep '^Found DFU'", shell=True)


# A04 hub ROMs carry their version at offset 0x10e, which the hub reports as bcdDevice:
# 1sthub GL3523 (05e3:0610/0620) 8816 + 8817; 2ndhub GL3590 (05e3:0625) 7054 + 7061, a chip the D6000 we tested does not have.
HUBS = {0x0620: {0x8816, 0x8817}, 0x0625: {0x7054, 0x7061}}


def other_chips():
    """Parts A04 does not flash through the controller: report only (read-only)."""
    for pid, want in HUBS.items():
        have = {d.bcdDevice for d in usb.core.find(find_all=True, idVendor=0x05E3, idProduct=pid)}
        if have:
            print("hub 05e3:%04x %-14s A04 %-14s %s" % (pid, "/".join("%04X" % b for b in sorted(have)),
                  "/".join("%04X" % b for b in sorted(want)), "ok" if have == want else "DIFFERENT (Genesys ISP, Windows only)"))
    for d in usb.core.find(find_all=True, idVendor=0x05E3, idProduct=0x0626):
        print("hub 05e3:0626 %04X           no firmware in A04 (nothing to update)" % d.bcdDevice)
    dl = usb.core.find(idVendor=0x17E9, idProduct=0x6006)
    if dl:
        print("DisplayLink 17e9:6006 %X.%02X   updated by the DisplayLink driver, not by A04" % (dl.bcdDevice >> 8, dl.bcdDevice & 0xFF))


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


def dfuse_erase(pid, alt, addr, size, page=0x400):
    """DfuSe page erase (DNLOAD block 0, [0x41, addr]) for every page, like Dell's erase phase.

    dfu-util 0.11 skips the erase on the Parade alt: its layout "0*001Ka,64*001Kg" starts with a
    zero-page read-only segment at 0x0 that dfu-util treats as covering the whole range.
    """
    d = usb.core.find(idVendor=VID, idProduct=pid)
    usb.util.claim_interface(d, 0)
    try:
        d.set_interface_altsetting(0, alt)
        for a in range(addr, addr + size, page):
            d.ctrl_transfer(0x21, 1, 0, 0, bytes([0x41]) + struct.pack("<I", a))  # DFU_DNLOAD
            while True:
                st = d.ctrl_transfer(0xA1, 3, 0, 0, 6)  # DFU_GETSTATUS
                if st[0] != 0:
                    sys.exit("erase at %#x failed: DFU status %d state %d" % (a, st[0], st[4]))
                if st[4] != 4:  # leave dfuDNBUSY
                    break
                time.sleep((st[1] | st[2] << 8 | st[3] << 16) / 1000.0)
        d.ctrl_transfer(0x21, 6, 0, 0, None)  # DFU_ABORT -> dfuIDLE for dfu-util
        print("erased %d pages at %#x" % (size // page, addr))
    finally:
        usb.util.release_interface(d, 0)
        usb.util.dispose_resources(d)


def flash(part):
    f, want, pid, alt, addr, _ = PARTS[part]
    path = bin_path(part)
    assert os.path.isfile(path), "run extract first"
    # writing alt0 from the app-DFU (6001) would overwrite the running app: refuse
    if dfu_pids() != [pid]:
        sys.exit("%s must be written on 06c4:%04x, present: %s" % (part, pid, ["%04x" % p for p in dfu_pids()]))
    dev = "06c4:%04x" % pid
    size = os.path.getsize(path)
    target = "%#x" % addr
    if part == "pr":
        dfuse_erase(pid, alt, addr, size)
        target += ":force"  # dfu-util would otherwise refuse the misparsed layout
    run(["dfu-util", "-d", dev, "-a", str(alt), "-s", target, "-D", path])
    if part == "st":
        return  # c302 cannot upload; the version after `leave c302` is the proof
    rb = path + ".readback"
    if os.path.exists(rb):
        os.remove(rb)
    run(["dfu-util", "-d", dev, "-a", str(alt), "-s", "%#x:%#x" % (addr, size), "-U", rb])
    ok = open(rb, "rb").read() == open(path, "rb").read()
    print("VERIFY", "OK" if ok else "MISMATCH")
    if not ok:
        sys.exit("%s readback differs: do NOT power off the dock, retry `flash %s`" % (part, part))


def wait_pid(pid, secs=60):
    for _ in range(secs):
        if pid == APP and versions() or pid != APP and dfu_pids() == [pid]:
            return
        time.sleep(1)
    sys.exit("timeout waiting for 06c4:%04x" % pid)


def to_mode(pid):
    """Walk c411 -> 6001 -> c302 (and back) until the controller is in the wanted mode."""
    for _ in range(4):
        if pid == APP and versions() or pid != APP and dfu_pids() == [pid]:
            return
        cur = dfu_pids()
        if not cur:
            detach()
            wait_pid(APPDFU)
            time.sleep(8)  # Dell waits 7.5 s after 6001 shows up
        else:
            leave(cur[0])
            time.sleep(2)
    sys.exit("could not reach 06c4:%04x" % pid)


def upgrade():
    v = versions()
    if not v:
        sys.exit("controller 06c4:c411 not found (dock plugged in and allowed by macOS?)")
    todo = [p for p in ("st", "ti", "pr") if v[p] != PARTS[p][1]]
    if not todo:
        print("everything already on A04")
        return
    extract()
    print("to update:", ", ".join("%s %s -> %s" % (p, v[p], PARTS[p][1]) for p in todo))
    if input("Dock on AC power, nothing else plugged into it? Type yes: ") != "yes":
        sys.exit("aborted")
    if "st" in todo:
        to_mode(BOOT)
        flash("st")
        to_mode(APP)
        if versions()["st"] != PARTS["st"][1]:
            sys.exit("controller did not come up with %s" % PARTS["st"][1])
        print("controller now", PARTS["st"][1])
    for p in ("ti", "pr"):
        if p in todo:
            to_mode(APPDFU)
            flash(p)
    to_mode(APP)
    print("Done. Now unplug the dock from AC and from the Mac, wait 30 s, plug AC first, then the Mac,")
    print("allow the accessory, and run `status` (TI and Parade load their firmware only at power-up).")


def main():
    a = sys.argv[1:] or ["status"]
    if a[0] == "status":
        status()
    elif a[0] == "upgrade":
        upgrade()
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
