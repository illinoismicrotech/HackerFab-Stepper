# Hacker Fab
# GRBL Stage Controller

import time

import serial

from stage_control.stage_controller import StageController, UnsupportedCommand

_SERIAL_TIMEOUT = 5.0   # seconds to wait for the 'ok' after a G-code line
_QUERY_TIMEOUT = 1.0    # a status report ('?') normally arrives within milliseconds
_HOMING_POLL_TIMEOUT = 0.15  # GRBL 1.1 does not answer '?' while homing: don't wait long for it
_HOMING_TIMEOUT = 120.0


class GrblStage(StageController):
    """Controls a GRBL / grblHAL stage over a serial port.

    Positions are tracked in the coordinates the controller reports: machine coordinates
    (MPos, GRBL's default) or work coordinates (WPos, if $10 is set that way). Absolute moves
    use the same coordinates (G53 for machine coordinates), so the app never sends the stage
    somewhere other than where its readout says. Jogs are relative moves (G91).
    """

    def __init__(self, controller_target, enable_homing: bool, invert_z: bool = False):
        self.controller_target = controller_target
        self.controller_target.timeout = _SERIAL_TIMEOUT
        self.enable_homing = enable_homing
        self.z_direction = -1.0 if invert_z else 1.0
        self.last_state = "Unknown"
        self.frame = "machine"          # "machine" (MPos) or "work" (WPos)
        self.last_position = None       # mm, in app coordinates (Z sign applied)
        self._homing = False
        self._homing_ok = False
        self._homing_saw_cycle = False
        self._homing_error = ""
        self._idle_without_ok_since = None
        self.fresh_report = False
        self._connect()

    def _connect(self) -> None:
        # Give GRBL time to boot (boards that reset when the port opens), then drop the banner.
        time.sleep(2.0)
        self.controller_target.reset_input_buffer()
        for attempt in range(3):
            idle, pos = self._query_state()
            if pos is not None or self.last_state != "Unknown":
                break
            time.sleep(0.5)
        else:
            try:
                self.controller_target.close()
            except Exception:
                pass
            raise ConnectionError("the controller did not answer a status request. Check that this port is the "
                                  "stage controller (GRBL) and that the controller has power")
        print(f"GRBL connected: state={self.last_state}, position={pos} mm ({self.frame} coordinates)")

    # ------------------------------------------------------------------ #
    # Internal communication
    # ------------------------------------------------------------------ #

    def _read_line(self):
        """One line without its line ending; '' for a blank line, None when nothing arrived in time."""
        raw = self.controller_target.read_until(b'\n')
        if not raw:
            return None
        return raw.rstrip(b'\r\n').decode('ascii', errors='replace')

    def _send_msg(self, msg: bytes) -> None:
        """Send a G-code line and wait for the 'ok' acknowledgement."""
        command = msg.decode('ascii', errors='replace').strip()
        self.controller_target.write(msg)
        for _ in range(20):
            raw = self.controller_target.read_until(b'\n')
            if not raw:
                print(f"GRBL > {command}  (no reply)")
                raise TimeoutError(f"GRBL timeout waiting for response to {msg!r}")
            line = raw.rstrip(b'\r\n')
            if not line:
                continue  # blank line (e.g. part of the start-up banner after a reset)
            if line == b'ok':
                print(f"GRBL > {command}  ok")   # every command lands in HackerfabStepper.log
                return
            if line.startswith(b'error:'):
                print(f"GRBL > {command}  {line.decode('ascii', errors='replace')}")
                raise RuntimeError(f"GRBL error: {line.decode('ascii', errors='replace')}")
            if line.startswith(b'ALARM'):
                print(f"GRBL > {command}  {line.decode('ascii', errors='replace')}")
                self.last_state = "Alarm"
                raise RuntimeError(f"GRBL {line.decode('ascii', errors='replace')}")
            # Skip bracketed info/status messages (e.g. [MSG:...], [GC:...])
            if line.startswith(b'[') or line.startswith(b'<'):
                continue
            print(f"GRBL: {line.decode('ascii', errors='replace')}")
        raise RuntimeError("GRBL: too many unexpected responses")

    def _query_state(self):
        """Send '?' and parse the status report. Returns (idle, position in mm or None)."""
        port = self.controller_target
        previous = port.timeout
        self.fresh_report = False
        try:
            port.timeout = _HOMING_POLL_TIMEOUT if self._homing else _QUERY_TIMEOUT
        except Exception:
            pass
        try:
            port.write(b'?')
            for _ in range(12):
                line = self._read_line()
                if line is None:
                    break
                if not line:
                    continue
                if line.startswith('<') and line.endswith('>'):
                    self.fresh_report = True
                    self.last_state = line[1:-1].split('|')[0] or "Unknown"
                    if self.last_state.startswith("Home"):
                        self._homing_saw_cycle = True
                    idle, position, frame = self._parse_state(line)
                    if frame:
                        self.frame = frame
                    if position is None:
                        return idle, None
                    # Keep readback in the same coordinate system as application moves.
                    self.last_position = (position[0], position[1], position[2] * self.z_direction)
                    return idle, self.last_position
                # Lines that were waiting in the buffer: a late 'ok' (from $H) or messages.
                if self._homing:
                    if line == 'ok':
                        self._homing_ok = True
                    elif line.startswith(('ALARM', 'error')):
                        self._homing_error = line
                elif line.startswith('ALARM'):
                    self.last_state = "Alarm"
        finally:
            try:
                port.timeout = previous
            except Exception:
                pass
        return False, None

    @staticmethod
    def _parse_state(resp: str):
        """Parse a status report like '<Idle|MPos:0.000,0.000,0.000|FS:0,0>'.

        Returns (idle, (x, y, z) in mm or None, "machine"/"work"/None)."""
        idle = False
        position, frame = None, None
        for part in resp.strip('<>').split('|'):
            if part.startswith('Idle'):
                idle = True
            elif part.startswith(('MPos:', 'WPos:')):
                try:
                    x, y, z = (float(v) for v in part[5:].split(',')[:3])
                    position = (x, y, z)
                    frame = "machine" if part.startswith('MPos') else "work"
                except ValueError:
                    pass
        return idle, position, frame

    def wait_for_idle(self, timeout: float = 60.0) -> bool:
        """Poll GRBL status until it reports Idle. Returns False if it did not within timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            idle, _ = self._query_state()
            if idle:
                return True
            time.sleep(0.05)
        return False

    def is_idle(self) -> bool:
        idle, _ = self._query_state()
        return idle

    def state_name(self) -> str:
        """Last reported GRBL state, e.g. Idle, Run, Jog, Hold:0, Alarm."""
        return self.last_state

    def position(self):
        """Current position in µm (app coordinates), or None if the controller did not say."""
        _idle, pos = self._query_state()
        if pos is None:
            pos = self.last_position   # the last report, e.g. from connecting
        if pos is None:
            return None
        return (pos[0] * 1000.0, pos[1] * 1000.0, pos[2] * 1000.0)

    # ------------------------------------------------------------------ #
    # StageController interface
    # ------------------------------------------------------------------ #

    def has_homing(self) -> bool:
        return self.enable_homing

    def begin_home(self) -> None:
        """Start the homing cycle without waiting for it (poll homing_finished)."""
        if not self.enable_homing:
            raise UnsupportedCommand()
        self.controller_target.reset_input_buffer()
        self._homing, self._homing_ok, self._homing_saw_cycle, self._homing_error = True, False, False, ""
        self._idle_without_ok_since = None
        self.last_state = "Home"   # forget a start-up Alarm: $H is how GRBL leaves it
        self.controller_target.write(b'$H\n')

    def homing_finished(self) -> bool:
        """True once homing has completed. Raises RuntimeError if the controller reports a failure.

        GRBL answers $H with 'ok' only when the whole cycle is done, and GRBL 1.1 does not answer
        '?' while homing, so: done = 'ok' received and a fresh Idle report. Waiting for the 'ok'
        also keeps it from being mistaken for the reply to the next command."""
        idle, _ = self._query_state()
        if self._homing_error or (self.fresh_report and self.last_state.startswith("Alarm")):
            self._homing = False
            raise RuntimeError(f"GRBL {self._homing_error or 'Alarm'} during homing")
        if idle and self.fresh_report:
            if self._homing_ok:
                self._homing = False
                return True
            # grblHAL reports 'Home' during the cycle; if it is Idle again but the 'ok' never
            # shows up, accept after 2 s and clear whatever is left in the buffer.
            if self._homing_saw_cycle:
                now = time.monotonic()
                self._idle_without_ok_since = self._idle_without_ok_since or now
                if now - self._idle_without_ok_since > 2.0:
                    self.controller_target.reset_input_buffer()
                    self._homing = False
                    return True
        return False

    def home(self) -> None:
        """Blocking homing (kept for scripts). The app uses begin_home / homing_finished."""
        self.begin_home()
        deadline = time.monotonic() + _HOMING_TIMEOUT
        time.sleep(1.0)
        while not self.homing_finished():
            if time.monotonic() > deadline:
                self._homing = False
                raise TimeoutError("homing did not finish within 2 minutes")
            time.sleep(0.25)

    def stop(self) -> None:
        """Emergency stop: GRBL soft reset (Ctrl-X). Motion stops at once; after a stop during a
        move or homing GRBL locks itself (Alarm) until it is homed or unlocked again."""
        self._homing = False
        try:
            self.controller_target.write(b'\x18')
            # The reset prints ALARM, a banner and messages: drop them so the next command
            # does not read them as its reply.
            time.sleep(0.5)
            self.controller_target.reset_input_buffer()
        except Exception:
            pass

    def _axes(self, microns: dict[str, float]) -> list[str]:
        parts = []
        if 'x' in microns:
            parts.append(f'X{microns["x"] / 1000.0:.4f}')
        if 'y' in microns:
            parts.append(f'Y{microns["y"] / 1000.0:.4f}')
        if 'z' in microns:
            parts.append(f'Z{microns["z"] * self.z_direction / 1000.0:.4f}')
        return parts

    def move_by(self, amounts: dict[str, float]) -> None:
        parts = self._axes(amounts)
        if not parts:
            return
        self._send_msg(b'G91\n')
        self._send_msg((' '.join(['G0'] + parts) + '\n').encode('ascii'))

    def move_to(self, amounts: dict[str, float]) -> None:
        parts = self._axes(amounts)
        if not parts:
            return
        self._send_msg(b'G90\n')
        prefix = ['G53', 'G0'] if self.frame == "machine" else ['G0']
        self._send_msg((' '.join(prefix + parts) + '\n').encode('ascii'))

    def reconnect(self) -> None:
        """Re-open the serial port after the controller dropped off USB (e.g. it reset)."""
        try:
            self.controller_target.close()
        except (OSError, serial.SerialException):
            pass
        self.controller_target.open()
        self.controller_target.timeout = _SERIAL_TIMEOUT
        self._homing = False
        self._connect()

    def close(self) -> None:
        if self.controller_target.is_open:
            self.controller_target.close()
