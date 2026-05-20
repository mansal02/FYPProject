"""Stub: multimodal vision is disabled in offline mode."""


class VisionWorker:
    """Disabled: multimodal vision is not used in offline mode."""

    def __init__(self, camera_index=0):
        self.camera_index = camera_index
        self.running = False

    def run(self):
        pass

    def stop(self):
        self.running = False
