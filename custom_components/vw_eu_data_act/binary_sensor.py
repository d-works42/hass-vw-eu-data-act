"""Binary sensor platform: curated boolean data points."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EudaConfigEntry
from .coordinator import EudaCoordinator
from .data import CURATED_BINARY, CuratedBinary, DataPoint
from .entity import EudaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EudaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    points: dict[str, DataPoint] = coordinator.data or {}
    present_fields = {dp.field_name for dp in points.values()}

    async_add_entities(
        EudaBinarySensor(coordinator, curated)
        for curated in CURATED_BINARY
        if curated.field_name in present_fields
    )


def _find_by_field(points: dict[str, DataPoint], field_name: str) -> DataPoint | None:
    """Pick a single point for a (possibly duplicated) field name.

    See sensor._find_by_field: the smallest UUID is chosen for a stable,
    deterministic selection across refreshes.
    """
    matches = [dp for dp in points.values() if dp.field_name == field_name]
    return min(matches, key=lambda dp: dp.key) if matches else None


class EudaBinarySensor(EudaEntity, BinarySensorEntity):
    """A curated boolean sensor."""

    def __init__(self, coordinator: EudaCoordinator, curated: CuratedBinary) -> None:
        super().__init__(coordinator)
        self._curated = curated
        self._attr_unique_id = f"{coordinator.vin}_{curated.field_name}"
        self._attr_name = curated.name
        if curated.icon:
            self._attr_icon = curated.icon
        if curated.device_class:
            self._attr_device_class = BinarySensorDeviceClass(curated.device_class)

    @property
    def is_on(self) -> bool | None:
        dp = _find_by_field(self.coordinator.data or {}, self._curated.field_name)
        result = None

        if dp is not None:
            val = dp.value

            # Handle boolean values
            if isinstance(val, bool):
                result = (not val) if self._curated.invert else val

            # Handle integer enum values (0/1=unavailable, 2/3=states)
            elif isinstance(val, int):
                # Special case: parking_brake uses simple 0/1 encoding
                if "parking_brake" in self._curated.field_name:
                    # 0=inactive (off), 1=active (on)
                    result = val == 1
                # 0=unsupported, 1=invalid -> unavailable (None)
                elif val in (0, 1):
                    result = None
                # For open_state/window_state/sunroof: 2=open, 3=closed
                # For locked_state: 2=locked, 3=unlocked
                # For safe_state: 2=safe, 3=unsafe
                elif val in (2, 3):
                    # Determine if 2=on or 3=on based on field naming
                    if (
                        "open_state" in self._curated.field_name
                        or "window_lifter" in self._curated.field_name
                        or "state_sunroof" in self._curated.field_name
                        or "state_of_hood" in self._curated.field_name
                        or "state_service_hatch" in self._curated.field_name
                        or "state_spoiler" in self._curated.field_name
                    ):
                        # 2=open (on), 3=closed (off)
                        is_active = val == 2
                    elif (
                        "locked_state" in self._curated.field_name
                        or "safe_state" in self._curated.field_name
                    ):
                        # 2=locked/safe, 3=unlocked/unsafe
                        # With invert=True: val==2 (locked) -> is_active=True -> inverted -> on (locked)
                        is_active = val == 2
                    else:
                        # Default: 2=off, 3=on
                        is_active = val == 3

                    result = (not is_active) if self._curated.invert else is_active

        return self._sticky(result)
