"""Camera discovery and advertised capture modes; no settings are changed here."""
from dataclasses import dataclass
from pathlib import Path
import platform
import re
import subprocess


@dataclass(frozen=True)
class CaptureMode:
    fourcc: str
    width: int
    height: int
    fps: float

    def label(self):
        return f"{self.width} × {self.height} · {self.fps:g} fps · {self.fourcc}"


@dataclass(frozen=True)
class CameraDevice:
    device: str
    name: str
    usb_speed: str = ""

    def connection_hint(self):
        if 'B0477' in self.name and self.usb_speed:
            try:
                if float(self.usb_speed) <= 480:
                    return ('Arducam B0477 is connected at USB 2 speed. Its supported fallback is '
                            '1280 × 720, YUYV, 10 fps. For full modes, reconnect directly with a USB 3 '
                            'data cable and USB 3 port, then refresh devices.')
            except ValueError:
                pass
        return ''

    def label(self):
        speed = f" · USB {self.usb_speed} Mb/s" if self.usb_speed else ""
        return f"{self.name} ({self.device}){speed}"


def v4l_info(device, option):
    try:
        result = subprocess.run(["v4l2-ctl", "--device", str(device), option],
                                capture_output=True, text=True, timeout=3)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def parse_modes(output):
    modes = []
    fourcc = None
    size = None
    for line in output.splitlines():
        fmt = re.search(r"\[\d+\]: '(.{4})'", line)
        resolution = re.search(r"Size: Discrete (\d+)x(\d+)", line)
        fps = re.search(r"\(([\d.]+) fps\)", line)
        if fmt:
            fourcc, size = fmt[1], None
        elif resolution:
            size = (int(resolution[1]), int(resolution[2]))
        elif fps and fourcc and size:
            modes.append(CaptureMode(fourcc, *size, float(fps[1])))
    return list(dict.fromkeys(modes))


def _msmf_device_names():
    """Camera names in the order OpenCV's Media Foundation backend numbers them.

    Uses MFEnumDeviceSources, the same call cv2.CAP_MSMF uses, so index i here is
    cv2.VideoCapture(i, cv2.CAP_MSMF). DirectShow order can differ (virtual cameras
    such as OBS appear only in DirectShow), so these names are for MSMF only.
    """
    import ctypes
    import uuid
    from ctypes import POINTER, byref, c_uint32, c_void_p, c_wchar_p

    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_ubyte * 16)]

        def __init__(self, text):
            super().__init__()
            ctypes.memmove(self.data, uuid.UUID(text).bytes_le, 16)

    source_type = GUID("c60ac5fe-252a-478f-a0ef-bc8fa5f7cad3")   # MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE
    video_capture = GUID("8ac3587a-4ae7-42d8-99e0-0a6013eef90f")  # ..._SOURCE_TYPE_VIDCAP_GUID
    friendly_name = GUID("60d0e559-52f8-4fa2-bbce-acdb34a8ec01")  # MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME

    def call(obj, index, argtypes, *args):
        # IMFAttributes vtable: 2 Release, 13 GetAllocatedString, 24 SetGUID
        vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        return ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(vtable[index])(obj, *args)

    ole32, mfplat, mf = ctypes.oledll.ole32, ctypes.oledll.mfplat, ctypes.oledll.mf
    ole32.CoTaskMemFree.restype = None
    ole32.CoTaskMemFree.argtypes = [c_void_p]
    com_started = False
    try:
        ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        com_started = True
    except OSError:
        pass  # COM already initialised on this thread in another mode; that is fine
    names = []
    attributes = c_void_p()
    devices = POINTER(c_void_p)()
    count = c_uint32()
    try:
        mfplat.MFStartup(0x00020070, 0)  # MF_VERSION, MFSTARTUP_FULL
        try:
            mfplat.MFCreateAttributes(byref(attributes), 1)
            call(attributes, 24, [c_void_p, c_void_p], byref(source_type), byref(video_capture))
            mf.MFEnumDeviceSources(attributes, byref(devices), byref(count))
            for i in range(count.value):
                text = c_wchar_p()
                length = c_uint32()
                try:
                    call(devices[i], 13, [c_void_p, c_void_p, c_void_p], byref(friendly_name), byref(text), byref(length))
                    names.append(text.value or f"Camera {i}")
                    ole32.CoTaskMemFree(ctypes.cast(text, c_void_p))
                except OSError:
                    names.append(f"Camera {i}")
                call(devices[i], 2, [])
            if devices:
                ole32.CoTaskMemFree(ctypes.cast(devices, c_void_p))
        finally:
            if attributes:
                call(attributes, 2, [])
            mfplat.MFShutdown()
    finally:
        if com_started:
            ole32.CoUninitialize()
    return names


def _windows_priority(name):
    """Lower sorts first: the Arducam, then other USB cameras, then laptop built-in cameras."""
    lowered = name.lower()
    if any(word in lowered for word in ("arducam", "imx283", "b0477")):
        return 0
    if any(word in lowered for word in ("integrated", "built-in", "internal", "front", "rear", "ir camera")):
        return 2
    return 1


def _dshow_device_names():
    """Camera names in DirectShow order, i.e. cv2.VideoCapture(i, cv2.CAP_DSHOW)."""
    from pygrabber.dshow_graph import FilterGraph
    return list(FilterGraph().get_input_devices())


def discover_devices(named=True, api="msmf"):
    if platform.system() == "Windows" and named:
        try:
            names = _dshow_device_names() if api == "dshow" else _msmf_device_names()
        except Exception:
            names = []
        if names:
            devices = [CameraDevice(str(i), name) for i, name in enumerate(names)]
            return sorted(devices, key=lambda d: (_windows_priority(d.name), int(d.device)))
    if platform.system() != "Linux":
        # OpenCV has no portable named-device enumeration. These are candidates,
        # validated in an isolated capture process when selected.
        return [CameraDevice(str(i), f"Camera index {i} (test on connect)") for i in range(8)]
    aliases = {}
    for path in sorted(Path("/dev/v4l/by-id").glob("*")):
        aliases.setdefault(str(path.resolve()), str(path))
    devices = []
    for node in sorted(Path("/sys/class/video4linux").glob("video*")):
        device = f"/dev/{node.name}"
        info = v4l_info(device, "--all")
        # Metadata-only nodes cannot deliver images. If v4l2-ctl is unavailable,
        # retain the candidate and let capture validate it.
        caps = info.split("Device Caps", 1)[-1]
        if info and "Video Capture" not in caps:
            continue
        try:
            name = (node / "name").read_text().strip()
        except OSError:
            name = node.name
        speed = ""
        for parent in (node / "device").resolve().parents:
            try:
                speed = (parent / "speed").read_text().strip()
                break
            except OSError:
                pass
        devices.append(CameraDevice(aliases.get(device, device), name, speed))
    return sorted(devices, key=lambda d: ("arducam" not in d.name.lower(), d.device))


def candidate_modes(device, settings):
    if settings.get("mode", "auto") == "manual":
        return [CaptureMode(settings.get("fourcc", "MJPG"), int(settings.get("width", 1280)),
                            int(settings.get("height", 720)), float(settings.get("fps", 30)))]
    modes = parse_modes(v4l_info(device, "--list-formats-ext")) if platform.system() == "Linux" else []
    # Prefer compressed USB transport, moderate image size and <=30 fps. Higher
    # resolutions remain available explicitly for calibrated imaging workflows.
    if modes:
        return sorted(modes, key=lambda m: (m.fourcc not in ("MJPG", "JPEG"),
                      m.width * m.height > 1280 * 960, m.fps > 30,
                      abs(m.width * m.height - 1280 * 720), abs(m.fps - 30)))[:16]
    return [CaptureMode(fmt, w, h, fps) for fmt in ("MJPG", "YUYV")
            for w, h, fps in ((1280, 720, 30), (640, 480, 30), (640, 480, 15))] + [None]
