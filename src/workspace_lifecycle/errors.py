class LifecycleError(RuntimeError):
    """A refused lifecycle operation; existing user data is left in place."""

    def __init__(self, message, recovery=None):
        super().__init__(message)
        self.recovery = recovery
