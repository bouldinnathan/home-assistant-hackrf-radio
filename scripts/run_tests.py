"""Import HA before pytest plugins so HA's HTTPX aliases initialize first."""

import sys

import homeassistant  # noqa: F401
import pytest

raise SystemExit(pytest.main(sys.argv[1:]))
