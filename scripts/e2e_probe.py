"""Temporary probe module for PR #60 end-to-end sync testing (throwaway branch)."""


class SyncE2EProbe:
    """A throwaway component so the incremental analysis has a real change to render."""

    def __init__(self, version: int) -> None:
        self.version = version

    def describe(self) -> str:
        return f"sync e2e probe v{self.version}"
