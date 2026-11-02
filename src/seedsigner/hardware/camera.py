import io
import logging

from gettext import gettext as _
from PIL import Image

from seedsigner.models.settings import Settings, SettingsConstants
from seedsigner.models.singleton import Singleton

logger = logging.getLogger(__name__)


class CameraConnectionError(Exception):
    pass



class Camera(Singleton):
    _video_stream = None
    _picamera = None
    _camera_rotation = None
    _zoom = 1           # SeedSigner Max: digital zoom of the video stream (see set_zoom)

    @classmethod
    def get_instance(cls):
        # This is the only way to access the one and only Controller
        if cls._instance is None:
            cls._instance = cls.__new__(cls)
        cls._instance._camera_rotation = int(Settings.get_instance().get_value(SettingsConstants.SETTING__CAMERA_ROTATION))
        return cls._instance


    def start_video_stream_mode(self, resolution=(512, 384), framerate=12, format="bgr"):
        from picamera import PiCameraError
        from seedsigner.hardware.pivideostream import PiVideoStream
        if self._video_stream is not None:
            self.stop_video_stream_mode()

        try:
            self._video_stream = PiVideoStream(resolution=resolution,framerate=framerate, format=format)
            self._video_stream.start()
        except PiCameraError:
            # This error most often occurs because the camera connection is loose
            raise CameraConnectionError()


    def read_video_stream(self, as_image=False, rgb=False):
        """
        `rgb` (SeedSigner Max, live preview): return the frame as an RGB image instead of
        RGBA. Same pixels, same rotation, but the display driver converts RGB straight to
        RGB565 while RGBA takes a much slower path on every frame.
        """
        if not self._video_stream:
            raise Exception("Must call start_video_stream first.")
        frame = self._video_stream.read()
        if not as_image:
            return frame
        else:
            if frame is not None:
                image = Image.fromarray(frame.astype('uint8'), 'RGB')
                if not rgb:
                    image = image.convert('RGBA')
                return image.rotate(90 + self._camera_rotation)
        return None


    @staticmethod
    def zoom_roi(factor: float) -> tuple[float, float, float, float]:
        """
        SeedSigner Max: the centered region of interest for a zoom `factor`, in
        picamera's normalized (x, y, w, h) sensor coordinates: 1 -> the whole frame,
        2 -> the middle half of each side, 3 -> the middle third.
        """
        factor = max(1.0, float(factor))
        side = 1.0 / factor
        offset = (1.0 - side) / 2.0
        return (offset, offset, side, side)


    @property
    def zoom(self):
        return self._zoom


    def set_zoom(self, factor):
        """
        SeedSigner Max: digital zoom on the running video stream, done by the camera's
        own image processor (picamera's `zoom` region of interest, MMAL crop) before the
        frame is scaled down to the capture size. It costs the Pi Zero's CPU nothing.

        It is real magnification, not an upscale: at 320 x 320 the sensor mode used for
        the scanner (1296 x 972, binned) gives ~3 sensor pixels per output pixel across
        the square field, so 2x still has ~1.5 and 3x ~1 real pixel per output pixel. A
        small QR code (an address in Sparrow's Receive tab) gets 2-3 times more pixels
        per module, which zbar needs, at the same distance from the screen.
        """
        self._zoom = factor
        if self._video_stream is not None:
            try:
                self._video_stream.camera.zoom = self.zoom_roi(factor)
            except Exception as e:
                logger.info(f"Camera zoom not applied: {e}")


    def stop_video_stream_mode(self):
        if self._video_stream is not None:
            self._video_stream.stop()
            self._video_stream = None


    def start_single_frame_mode(self, resolution=(720, 480)):
        from picamera import PiCamera, PiCameraError
        if self._video_stream is not None:
            self.stop_video_stream_mode()
        if self._picamera is not None:
            self._picamera.close()

        try:
            self._picamera = PiCamera(resolution=resolution, framerate=24)
            self._picamera.start_preview()
        except PiCameraError:
            # This error most often occurs because the camera connection is loose
            raise CameraConnectionError()


    def capture_frame(self):
        if self._picamera is None:
            raise Exception("Must call start_single_frame_mode first.")

        # Set auto-exposure values
        self._picamera.shutter_speed = self._picamera.exposure_speed
        self._picamera.exposure_mode = 'off'
        g = self._picamera.awb_gains
        self._picamera.awb_mode = 'off'
        self._picamera.awb_gains = g

        stream = io.BytesIO()
        self._picamera.capture(stream, format='jpeg')

        # "Rewind" the stream to the beginning so we can read its content
        stream.seek(0)
        return Image.open(stream).rotate(90 + self._camera_rotation)


    def stop_single_frame_mode(self):
        if self._picamera is not None:
            self._picamera.close()
            self._picamera = None

