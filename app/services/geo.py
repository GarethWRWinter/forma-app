"""Offline reverse geocoding for ride locales.

The rider's coordinates never leave the server: reverse_geocoder resolves
against a bundled city dataset in-process. Coarse (nearest town) is exactly
the right grain for a coach — "the Brighton roads", not a street address.
"""

import logging

logger = logging.getLogger(__name__)

_rc = None


def _engine():
    global _rc
    if _rc is None:
        import reverse_geocoder

        _rc = reverse_geocoder
    return _rc


# The rider reads this, so spell countries like a human. Everything else
# falls back to the region name, which reads well for most of the world.
_COUNTRY = {
    "GB": "UK", "US": "USA", "FR": "France", "IT": "Italy", "ES": "Spain",
    "BE": "Belgium", "NL": "Netherlands", "DE": "Germany", "CH": "Switzerland",
    "AT": "Austria", "PT": "Portugal", "DK": "Denmark", "NO": "Norway",
    "SE": "Sweden", "IE": "Ireland", "AU": "Australia", "NZ": "New Zealand",
    "CA": "Canada", "EE": "Estonia", "MY": "Malaysia", "AE": "UAE",
    "ZA": "South Africa", "JP": "Japan", "CO": "Colombia", "AD": "Andorra",
}


def locale_for(lat: float, lon: float) -> str | None:
    """Nearest-town locale like "Ditchling, UK". None when lookup fails."""
    try:
        hit = _engine().search([(lat, lon)], mode=1)[0]
        name = hit.get("name") or hit.get("admin2") or hit.get("admin1")
        cc = hit.get("cc") or ""
        country = _COUNTRY.get(cc, hit.get("admin1") or cc)
        if not name:
            return None
        return f"{name}, {country}" if country and country != name else name
    except Exception:
        logger.exception("Reverse geocode failed for (%s, %s)", lat, lon)
        return None
