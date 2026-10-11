# Hacker Fab
# J. Kent Wirant
# Stage Controller Interface


class UnsupportedCommand(Exception):
    pass


class StageController:
    """Stage interface. This base class is the simulated stage: it accepts moves and does nothing."""

    def move_by(self, amounts: dict[str, float]):
        """Relative move, µm per axis."""

    def move_to(self, amounts: dict[str, float]):
        """Absolute move, µm per axis, in the coordinates position() reports."""

    def position(self):
        """Current (x, y, z) in µm, or None when the stage cannot say (the simulated stage)."""
        return None

    def has_homing(self):
        return False

    def is_idle(self):
        """True when the stage is not moving. The simulated stage is always idle."""
        return True

    def state_name(self):
        return "Idle"

    def home(self):
        raise UnsupportedCommand()

    def stop(self):
        pass

    def close(self):
        pass
