"""The Overkiz (by Somfy) integration."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass

from aiohttp import ClientConnectorError, ClientError, ServerDisconnectedError
from pyoverkiz.client import OverkizClient
from pyoverkiz.const import SUPPORTED_SERVERS
from pyoverkiz.enums import APIType
from pyoverkiz.exceptions import (
    BadCredentialsException,
    MaintenanceException,
    NotSuchTokenException,
    TooManyRequestsException,
)
from pyoverkiz.models import Device, Scenario
from pyoverkiz.utils import generate_local_server

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_TOKEN,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import (
    CONF_API_TYPE,
    CONF_HUB,
    CONF_SERVER,
    DOMAIN,
    LOGGER,
    OVERKIZ_DEVICE_TO_PLATFORM,
    PLATFORMS,
    UPDATE_INTERVAL,
    UPDATE_INTERVAL_ALL_ASSUMED_STATE,
)
from .coordinator import OverkizDataUpdateCoordinator


@dataclass
class HomeAssistantOverkizData:
    """Overkiz data stored in the Home Assistant data object."""

    coordinator: OverkizDataUpdateCoordinator
    platforms: defaultdict[Platform, list[Device]]
    scenarios: list[Scenario]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry."""

    LOGGER.debug("Migrating from version %s", entry.version)

    # v1 -> v2: CONF_HUB renamed to CONF_SERVER and CONF_API_TYPE added
    if entry.version == 1:
        v2_entry_data = {**entry.data}
        v2_entry_data[CONF_SERVER] = entry.data[CONF_HUB]
        v2_entry_data.pop(CONF_HUB)
        v2_entry_data[CONF_API_TYPE] = APIType.CLOUD  # V1 only supports cloud

        entry.version = 2
        hass.config_entries.async_update_entry(entry, data=v2_entry_data)

        LOGGER.debug("Migration to version %s successful", entry.version)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Overkiz from a config entry."""

    client: OverkizClient | None = None

    # Local API
    if entry.data[CONF_API_TYPE] == APIType.LOCAL:
        host = entry.data[CONF_HOST]
        token = entry.data[CONF_TOKEN]

        # Verify SSL blocked by https://github.com/Somfy-Developer/Somfy-TaHoma-Developer-Mode/issues/5
        # Somfy (self-signed) SSL cert uses the wrong common name
        session = async_create_clientsession(hass, verify_ssl=False)

        client = OverkizClient(
            username="",
            password="",
            token=token,
            session=session,
            server=generate_local_server(host=host),
        )
    # Overkiz Cloud API
    else:
        username = entry.data[CONF_USERNAME]
        password = entry.data[CONF_PASSWORD]
        server = SUPPORTED_SERVERS[entry.data[CONF_SERVER]]

        # To allow users with multiple accounts/hubs, we create a new session so they have separate cookies
        session = async_create_clientsession(hass)
        client = OverkizClient(
            username=username, password=password, session=session, server=server
        )

    try:
        await client.login()

        setup, scenarios = await asyncio.gather(
            *[
                client.get_setup(),
                client.get_scenarios(),
            ]
        )
    except (BadCredentialsException, NotSuchTokenException) as exception:
        raise ConfigEntryAuthFailed("Invalid authentication") from exception
    except TooManyRequestsException as exception:
        raise ConfigEntryNotReady("Too many requests, try again later") from exception
    except (
        TimeoutError,
        ClientError,
        ClientConnectorError,
        ServerDisconnectedError,
    ) as exception:
        raise ConfigEntryNotReady("Failed to connect") from exception
    except MaintenanceException as exception:
        raise ConfigEntryNotReady("Server is down for maintenance") from exception

    coordinator = OverkizDataUpdateCoordinator(
        hass,
        LOGGER,
        name="device events",
        client=client,
        devices=setup.devices,
        places=setup.root_place,
        update_interval=UPDATE_INTERVAL,
        config_entry_id=entry.entry_id,
    )

    await coordinator.async_config_entry_first_refresh()

    if coordinator.is_stateless:
        LOGGER.debug(
            (
                "All devices have an assumed state. Update interval has been reduced"
                " to: %s"
            ),
            UPDATE_INTERVAL_ALL_ASSUMED_STATE,
        )
        coordinator.update_interval = UPDATE_INTERVAL_ALL_ASSUMED_STATE

    platforms: defaultdict[Platform, list[Device]] = defaultdict(list)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = HomeAssistantOverkizData(
        coordinator=coordinator, platforms=platforms, scenarios=scenarios
    )

    # Map Overkiz entities to Home Assistant platform
    for device in coordinator.data.values():
        LOGGER.debug(
            (
                "The following device has been retrieved. Report an issue if not"
                " supported correctly (%s)"
            ),
            device,
        )

        if platform := OVERKIZ_DEVICE_TO_PLATFORM.get(
            device.widget
        ) or OVERKIZ_DEVICE_TO_PLATFORM.get(device.ui_class):
            platforms[platform].append(device)

    device_registry = dr.async_get(hass)

    for gateway in setup.gateways:
        LOGGER.debug("Added gateway (%s)", gateway)

        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, gateway.id)},
            model=gateway.sub_type.beautify_name if gateway.sub_type else None,
            manufacturer=client.server.manufacturer,
            name=gateway.type.beautify_name if gateway.type else gateway.id,
            sw_version=gateway.connectivity.protocol_version,
            configuration_url=client.server.configuration_url,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
