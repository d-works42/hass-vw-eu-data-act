"""Coordinator: dynamic-interval refresh of the latest dataset."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ApiError, AuthError, EudaApiClient
from .const import (
    CONF_IDENTIFIER,
    CONF_VIN,
    DATASET_INTERVAL,
    DOMAIN,
    MIN_INTERVAL,
    NO_CONTENT_SUFFIX,
    POST_DATASET_BUFFER,
    RETRY_INTERVAL,
)
from .data import Dataset, DataPoint

_LOGGER = logging.getLogger(__name__)


def _filename_timestamp(name: str) -> datetime | None:
    """Parse a YYYYMMDDhhmmss segment from a dataset filename.

    Handles both layouts seen in the wild ("TIMESTAMP_VIN.zip" and
    "VIN_TIMESTAMP.zip") by scanning the underscore-separated parts
    right-to-left for the first one that parses as a timestamp.
    """
    stem = name.rsplit(".", 1)[0]
    for part in reversed(stem.split("_")):
        try:
            return datetime.strptime(part, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _created_on(entry: dict) -> datetime | None:
    raw = entry.get("createdOn")
    if not raw:
        return _filename_timestamp(entry.get("name", ""))
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return _filename_timestamp(entry.get("name", ""))


class EudaCoordinator(DataUpdateCoordinator[dict[str, DataPoint]]):
    """Fetches the latest dataset and reschedules adaptively."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: EudaApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.data[CONF_VIN]}",
            update_interval=RETRY_INTERVAL,
        )
        self.entry = entry
        self.client = client
        self.vin: str = entry.data[CONF_VIN]
        self.identifier: str = entry.data[CONF_IDENTIFIER]
        self.latest_dataset: Dataset | None = None

    async def _async_update_data(self) -> dict[str, DataPoint]:
        try:
            listing = await self.client.async_list_datasets(self.vin, self.identifier)
        except AuthError as err:
            # Retry soon rather than waiting the full ~15-min cadence.
            self.update_interval = RETRY_INTERVAL
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except ApiError as err:
            self.update_interval = RETRY_INTERVAL
            if "HTTP 400" in str(err):
                # The data-delivery endpoint returns 400 until the portal has
                # finished provisioning a newly enabled continuous data request,
                # which can take a few hours. HA keeps retrying until it's ready.
                raise UpdateFailed(
                    "Data delivery not ready yet (HTTP 400). If you just enabled "
                    "the continuous data request on the portal, it can take a few "
                    "hours to start; will keep retrying."
                ) from err
            raise UpdateFailed(str(err)) from err

        # content datasets, oldest -> newest by createdOn
        content = sorted(
            (e for e in listing if e.get("name") and not e["name"].endswith(NO_CONTENT_SUFFIX)),
            key=lambda e: _created_on(e) or datetime.min.replace(tzinfo=timezone.utc),
        )
        _LOGGER.debug("refresh: %d listed, %d with content", len(listing), len(content))

        if not content:
            self._reschedule(listing)
            if self.data:
                return self.data
            raise UpdateFailed("No datasets with content available yet")

        # Load the newest dataset for live state. (We don't backfill history into
        # statistics: importing into recorder-managed sensor entities collides
        # with the recorder's own statistics and can corrupt unrelated ones.)
        newest = content[-1]
        try:
            payload = await self.client.async_download_dataset(
                self.vin, self.identifier, newest["name"]
            )
            self.latest_dataset = Dataset.from_json(payload)
        except ApiError as err:
            self.update_interval = RETRY_INTERVAL  # retry soon, not next cadence
            if self.data:
                _LOGGER.warning("Could not download newest %s: %s", newest["name"], err)
                return self.data
            raise UpdateFailed(f"Could not download newest dataset: {err}") from err

        self._reschedule(listing)
        return self.latest_dataset.points

    def _reschedule(self, listing: list[dict]) -> None:
        """Schedule the next poll for ~15 min after the newest known dataset.

        If that time has already passed (a new dataset is due but not yet
        present), poll every minute until it appears.
        """
        timestamps = [ts for e in listing if (ts := _created_on(e))]
        newest = max(timestamps) if timestamps else None
        if newest:
            target = newest + DATASET_INTERVAL + POST_DATASET_BUFFER
            delta = target - dt_util.utcnow()
            if delta > MIN_INTERVAL:
                self.update_interval = delta
                _LOGGER.debug("Next refresh in %s (after newest %s)", delta, newest)
                return
        # newest dataset is overdue (or unknown) -> short retry for the next drop
        self.update_interval = RETRY_INTERVAL
        _LOGGER.debug("Next dataset overdue; retrying in %s", RETRY_INTERVAL)
