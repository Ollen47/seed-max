"""
SeedSigner Max v5: QR scanner zoom (camera ISP crop) for small QR codes.

The decode test simulates the camera chain on a small address QR code, the case found
with Sparrow's Receive tab (2 Nov 2026): the sensor sees the screen through the lens
(a little blur), the image processor crops the zoom region and scales it down to the
320 x 320 capture, and zbar (the same pyzbar SeedSigner uses) decodes the frame.
"""
from types import SimpleNamespace

import pytest
from PIL import Image, ImageFilter

from seedsigner.hardware import hw_profile
from seedsigner.hardware.camera import Camera


# ---------------------------------------------------------------------------------------
# Zoom region and Camera.set_zoom
# ---------------------------------------------------------------------------------------
def test_zoom_roi():
    assert Camera.zoom_roi(1) == (0.0, 0.0, 1.0, 1.0)
    assert Camera.zoom_roi(2) == (0.25, 0.25, 0.5, 0.5)
    x, y, w, h = Camera.zoom_roi(3)
    assert w == h == pytest.approx(1 / 3) and x == y == pytest.approx(1 / 3)
    assert Camera.zoom_roi(0.5) == (0.0, 0.0, 1.0, 1.0), "never wider than the full field"


def test_set_zoom_applies_to_the_running_stream():
    camera = Camera.__new__(Camera)
    camera._video_stream = None
    camera.set_zoom(2)
    assert camera.zoom == 2, "remembered with no stream running"

    picam = SimpleNamespace(zoom=(0.0, 0.0, 1.0, 1.0))
    camera._video_stream = SimpleNamespace(camera=picam)
    camera.set_zoom(3)
    assert picam.zoom == Camera.zoom_roi(3) and camera.zoom == 3
    camera.set_zoom(1)
    assert picam.zoom == (0.0, 0.0, 1.0, 1.0)


def test_set_zoom_never_breaks_the_scan():
    class Broken:
        @property
        def zoom(self):
            return None

        @zoom.setter
        def zoom(self, value):
            raise RuntimeError("MMAL error")

    camera = Camera.__new__(Camera)
    camera._video_stream = SimpleNamespace(camera=Broken())
    camera.set_zoom(2)                       # logged, not raised
    assert camera.zoom == 2


def test_zoom_settings():
    from seedsigner.models.settings_definition import SettingsConstants, SettingsDefinition
    entry = SettingsDefinition.get_settings_entry(SettingsConstants.SETTING__MAX_CAMERA_ZOOM)
    assert [v for v, _ in entry.selection_options] == list(hw_profile.CAMERA_ZOOM_STEPS) == [1, 2, 3]
    assert entry.default_value == hw_profile.CAMERA_ZOOM == 1, "starts as the stock scanner"
    assert entry.visibility == SettingsConstants.VISIBILITY__HARDWARE


# ---------------------------------------------------------------------------------------
# Why: a small QR code through the simulated camera chain
# ---------------------------------------------------------------------------------------
SENSOR = 972          # square field of the 1296 x 972 sensor mode
CAPTURE = 320         # hw_profile.CAMERA_RESOLUTION


def address_qr() -> Image.Image:
    import qrcode
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4, box_size=10)
    qr.add_data("bitcoin:bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq")
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("L")


def camera_frame(qr_sensor_px: int, zoom: float) -> Image.Image:
    """
    The QR code covers `qr_sensor_px` sensor pixels (quiet zone included) in the middle
    of a mid-grey screen; the lens blurs by ~1 sensor pixel; the image processor crops
    the zoom region and scales it to the 320 x 320 capture (area average).
    """
    scene = Image.new("L", (SENSOR, SENSOR), 90)
    qr = address_qr().resize((qr_sensor_px, qr_sensor_px), Image.Resampling.LANCZOS)
    scene.paste(qr, ((SENSOR - qr_sensor_px) // 2, (SENSOR - qr_sensor_px) // 2))
    scene = scene.filter(ImageFilter.GaussianBlur(1.0))
    x, y, w, h = Camera.zoom_roi(zoom)
    box = tuple(int(round(v * SENSOR)) for v in (x, y, x + w, y + h))
    return scene.crop(box).resize((CAPTURE, CAPTURE), Image.Resampling.BOX)


def decodes(frame: Image.Image) -> bool:
    from pyzbar import pyzbar
    return any(r.data.startswith(b"bitcoin:bc1q") for r in pyzbar.decode(frame))


def test_small_qr_needs_the_zoom():
    """
    An address QR code a sixth of the field wide (~162 sensor px, ~53 px in the
    320 px capture: ~1.2 capture px per module) is lost at 1x and read at 2x and 3x.
    A large one is read at 1x, as before.
    """
    try:
        import pyzbar.pyzbar  # noqa: F401
    except Exception:
        pytest.skip("zbar not available")
    modules = address_qr().size[0] // 10
    small = SENSOR // 6
    assert CAPTURE * small / SENSOR / modules < 1.5, "the test case really is too small at 1x"
    assert not decodes(camera_frame(small, 1))
    assert decodes(camera_frame(small, 2))
    assert decodes(camera_frame(small, 3))
    assert decodes(camera_frame(SENSOR // 2, 1)), "a large QR code still reads at 1x"
