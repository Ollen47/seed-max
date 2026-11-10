"""
v7.5: the microSD read-only while the verifier runs.

SeedSigner OS (v0.8.7) mounts the card's FAT partition read-WRITE at every boot
(etc/mdev/mdev.sh: `mount -o sync $DEVNAME /mnt/microsd`, run by the `mdev -df`
coldplug). The verifier never writes anything, and the independent review of 10 Nov 2026
confirmed it (strace, audit hooks, crashes provoked on purpose), but a writable card is
still one bug away from a write. It is also why the FAT "volume dirty" flag (boot sector
byte 0x25) was found set: the kernel sets it on a read-write mount and clears it only on
a clean unmount, which switching off with the UPS HAT never does.

So, before anything else: remount it read-only. On a card that was clean, the remount
also clears the dirty flag again (fat_remount -> fat_set_state(sb, 0, 0)). From then on
any write to the card fails with EROFS. The one stronger protection is the card's own
write-protect; root code can always reach /dev/mmcblk0 directly.

ctypes and mount(2): no shell, no subprocess.
"""
import ctypes
import logging
import os
import time

logger = logging.getLogger(__name__)

MOUNTPOINT = "/mnt/microsd"
MS_RDONLY = 1
MS_REMOUNT = 32

STATUS__READ_ONLY = "read-only"
STATUS__WRITABLE = "WRITABLE"
STATUS__ABSENT = "not mounted"


def mount_options(mountpoint: str = MOUNTPOINT, mounts: str = "/proc/mounts"):
    """ The options of `mountpoint` in /proc/mounts (a list), or None if not mounted """
    try:
        with open(mounts) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    found = None
    for line in lines:
        fields = line.split()
        if len(fields) >= 4 and fields[1] == mountpoint:
            found = fields[3].split(",")           # the last mount on top wins
    return found


def _remount_read_only(mountpoint: str) -> int:
    # CDLL(None): the symbols already in the process (libc included). find_library would
    # run `ldconfig -p` in a subprocess
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p)
    if libc.mount(None, mountpoint.encode(), None, MS_REMOUNT | MS_RDONLY, None) != 0:
        return ctypes.get_errno()
    return 0


def protect_microsd(mountpoint: str = MOUNTPOINT, mounts: str = "/proc/mounts", remount=_remount_read_only) -> str:
    """ Remount the card read-only if it is mounted; the resulting STATUS__* """
    options = mount_options(mountpoint, mounts)
    if options is None:
        return STATUS__ABSENT
    if "ro" in options:
        return STATUS__READ_ONLY
    try:
        error = remount(mountpoint)
    except Exception as e:
        logger.info(f"microSD remount: {type(e).__name__}")
        error = -1
    if error:
        logger.info(f"microSD remount read-only failed: {os.strerror(error) if error > 0 else 'error'}")
    options = mount_options(mountpoint, mounts)
    if options is None:
        return STATUS__ABSENT
    return STATUS__READ_ONLY if "ro" in options else STATUS__WRITABLE


class MicroSDGuard:
    """
    v7.5 review: one check at start is not enough. SeedSigner OS starts the app before
    the mdev coldplug mounts the card, and mounts it again (read-write) if it is pulled
    out and put back. Called from every screen loop: at most once a second, the card is
    remounted read-only whenever it is found mounted read-write. Reading /proc/mounts
    costs microseconds.
    """
    def __init__(self, every_s: float = 1.0, on_change=None, mounts: str = "/proc/mounts",
                 remount=_remount_read_only, clock=time.monotonic):
        self.every_s, self.on_change, self.mounts, self.remount, self.clock = every_s, on_change, mounts, remount, clock
        self.last = None
        self.status = None

    def tick(self):
        now = self.clock()
        if self.last is not None and now - self.last < self.every_s:
            return self.status
        self.last = now
        status = protect_microsd(MOUNTPOINT, self.mounts, self.remount)
        if status != self.status:
            self.status = status
            logger.info(f"microSD: {status}")
            if self.on_change:
                self.on_change(status)
        return status
