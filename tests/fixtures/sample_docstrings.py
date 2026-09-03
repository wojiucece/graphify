"""Module docstring: coordinates the nightly settlement reconciliation pipeline."""


class Widget:
    """A widget class that renders itself."""

    def __init__(self, name: str):
        """Build the widget instance."""
        self.name = name

    @property
    def label(self) -> str:
        """Decorated property docstring is attached to the method node."""
        return self.name

    def render(self):
        """Render the widget.

        Multi-paragraph docstring, kept verbatim.
        """
        return self.name

    def local(self):
        def inner():
            """Local function docstring, inner is never a node."""
            return 1
        return inner()


def module_fn():
    """A module-level function with a docstring."""
    return 1


class FirstStatement:
    def concat(self):
        """Part one.""" """Part two."""
        return 1

    def formatted(self):
        f"""Formatted {name} docstring, f-prefix disqualifies."""
        return 1

    def assigned(self):
        x = """Assignment first statement, not a docstring."""
        return x

    def short(self):
        """ab"""
        return 1
