"""Read the Microsoft Store license for the packaged edition."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class StoreLicense:
    active: bool
    trial_until: datetime | None = None

    def allows_access(self):
        return self.active and (
            self.trial_until is None or datetime.now(timezone.utc) < self.trial_until
        )


def read_license():
    from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
    from winrt.windows.services.store import StoreContext

    async def get_license():
        license = await StoreContext.get_default().get_app_license_async()
        return StoreLicense(license.is_active, license.expiration_date if license.is_trial else None)

    init_apartment(ApartmentType.MULTI_THREADED)
    try:
        # Windows returns its cached license when the computer is offline.
        return asyncio.run(asyncio.wait_for(get_license(), timeout=10))
    finally:
        uninit_apartment()
