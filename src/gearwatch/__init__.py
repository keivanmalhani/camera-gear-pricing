"""gearwatch: used camera gear price tracking from official marketplace APIs.

Asking prices are noise. Sold prices are signal. gearwatch pulls completed-sale
comparables from official APIs only, builds a per-model price band, and scores a
live listing against that model's own recent sold distribution.

No scraping. No HTML parsing of any marketplace. No headless browser. Ever.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
