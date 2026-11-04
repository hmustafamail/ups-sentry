"""UPS monitoring for portable power stations: outage alerts, a better time-left estimate, and ECO mode kept off."""

import logging

__version__ = "0.1.0"

# A library stays quiet unless the application sets up logging; `ups-sentry run` does.
logging.getLogger(__name__).addHandler(logging.NullHandler())
