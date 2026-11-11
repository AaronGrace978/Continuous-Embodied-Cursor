# SPDX-License-Identifier: AGPL-3.0-or-later
"""Select the native desktop without importing platform-specific dependencies."""
import sys


def open_session(**kwargs):
    if sys.platform == "win32":
        from .windows import open_session as native
    else:
        from .linux import open_session as native
    return native(**kwargs)
