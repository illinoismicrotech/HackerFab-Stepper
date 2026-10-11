from PIL import Image

DEFAULT_SIZE = (1920, 1080)


class ProjectorController:
    def show(self, image: Image.Image):
        pass

    def size(self) -> tuple[int, int]:
        return DEFAULT_SIZE

    def clear(self):
        pass


class TkProjector(ProjectorController):
    """What the projector should show, at the projector's own resolution.

    Images are prepared at exactly the projector display's size (set from the display the
    projector window is on), so the projector window shows them pixel for pixel. on_show is
    called after every change; the main window forwards the image to the projector window
    and the previews.
    """

    def __init__(self, root=None, title='Projector', background='#000000', size=DEFAULT_SIZE):
        self._size = (int(size[0]), int(size[1]))
        self.current_image = Image.new('RGB', self._size, 'black')
        self.on_show = None

    def size(self) -> tuple[int, int]:
        return self._size

    def set_size(self, size: tuple[int, int]):
        self._size = (max(1, int(size[0])), max(1, int(size[1])))

    def show(self, image: Image.Image):
        self.current_image = image
        if self.on_show:
            self.on_show()

    def clear(self):
        self.show(Image.new('RGB', self.size(), 'black'))
