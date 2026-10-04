# Hacker Fab
# J. Kent Wirant
# Stage Controller Interface


class UnsupportedCommand(Exception):
    pass


# This will be an abstract interface for stage positioning.
class StageController:
    def move_by(self, amounts: dict[str, float]):
        print(f"ignoring move_by {amounts} in dummy stage controller")

    def move_to(self, amounts: dict[str, float]):
        print(f"ignoring move_to {amounts} in dummy_stage controller")

    def has_homing(self):
        return False

    def is_idle(self):
        """True when the stage is not moving. The simulated stage is always idle."""
        return True

    def state_name(self):
        return "Idle"

    def home(self):
        raise UnsupportedCommand()

    def position_um(self):
        """Current position in micrometers as (x, y, z), or None if unknown."""
        return None

    def reconnect(self):
        """Re-open the connection after it was lost. The simulated stage never disconnects."""

    def close(self):
        pass
