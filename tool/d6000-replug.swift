import Foundation
import IOUSBHost
// Software replug of a Dell D6000 (needs root for device capture).
// Re-enumerates the dock's top-level Genesys hub; resetting the USB2 side (05e3:0610) re-enumerates the
// whole dock incl. the USB3 side (same chip), so stop after the first success. 05e3:0620 is the fallback.
func prop(_ s: io_service_t, _ k: String) -> Int {
    IORegistryEntryCreateCFProperty(s, k as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue() as? Int ?? -1
}
func topHub(_ pid: Int) -> io_service_t? {
    var it: io_iterator_t = 0
    guard IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("IOUSBHostDevice"), &it) == KERN_SUCCESS else { return nil }
    defer { IOObjectRelease(it) }
    while case let s = IOIteratorNext(it), s != 0 {
        var parent: io_registry_entry_t = 0
        IORegistryEntryGetParentEntry(s, kIOServicePlane, &parent)
        var cls = [CChar](repeating: 0, count: 128)
        IOObjectGetClass(parent, &cls)
        IOObjectRelease(parent)
        // nested hubs sit under AppleUSB*HubPort, the dock's top hub under an XHCI root port
        if prop(s, "idVendor") == 0x05e3, prop(s, "idProduct") == pid, String(cString: cls).contains("XHCI") { return s }
        IOObjectRelease(s)
    }
    return nil
}
for pid in [0x0610, 0x0620] {
    // the hub can be busy for a moment (exclusive open fails): retry
    for attempt in 1...5 {
        guard let s = topHub(pid) else { break }
        defer { IOObjectRelease(s) }
        do {
            let dev = try IOUSBHostDevice(__ioService: s, options: [.deviceCapture], queue: nil, interestHandler: nil)
            try dev.reset()
            print(String(format: "re-enumerated D6000 hub 05e3:%04x", pid))
            exit(0)
        } catch {
            print(String(format: "05e3:%04x attempt %d: ", pid, attempt) + error.localizedDescription)
            sleep(1)
        }
    }
}
print("D6000 hub not found or busy (dock unplugged? run as root?)")
exit(1)
