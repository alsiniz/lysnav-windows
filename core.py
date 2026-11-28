"""DIRIGERA hub adapter and deny-by-default command authorization."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import math
import os
import tempfile
from typing import Any

UNASSIGNED = "__unassigned__"
ALL_APPROVED = "__all_approved__"
ACTIONS = {"power": "isOn", "brightness": "lightLevel", "temperature": "colorTemperature", "color": "colorHue"}


def room_id(device: Any) -> str:
    room = getattr(device, "room", None)
    rid = getattr(room, "id", None) if room is not None else None
    return str(rid) if rid else UNASSIGNED


def to_kelvin(value: Any) -> int | None:
    if value is None:
        return None
    try:
        val = float(value)
        if val <= 0:
            return None
        return round(1_000_000 / val) if val < 1000 else round(val)
    except (TypeError, ValueError, OverflowError):
        return None


def temp_range(attributes: Any) -> tuple[int, int] | None:
    lo = to_kelvin(getattr(attributes, "color_temperature_min", None))
    hi = to_kelvin(getattr(attributes, "color_temperature_max", None))
    return (min(lo, hi), max(lo, hi)) if lo is not None and hi is not None else None


@dataclass(frozen=True)
class LightInfo:
    id: str
    name: str
    room_id: str
    room_name: str
    reachable: bool
    capabilities: tuple[str, ...]
    on: bool
    brightness: int
    temperature: int | None
    temp_low: int | None
    temp_high: int | None
    hue: float
    saturation: float
    color_mode: str | None = None

    def can(self, action: str) -> bool:
        if action == "color":
            return "colorHue" in self.capabilities and "colorSaturation" in self.capabilities
        if action == "temperature":
            return "colorTemperature" in self.capabilities and self.temp_low is not None
        return ACTIONS[action] in self.capabilities


def light_info(light: Any) -> LightInfo:
    attrs = light.attributes
    caps = tuple(getattr(light.capabilities, "can_receive", ()) or ())
    rng = temp_range(attrs)
    color_mode = getattr(attrs, "color_mode", None)
    return LightInfo(
        id=str(light.id),
        name=str(getattr(attrs, "custom_name", "") or "Unnamed lamp"),
        room_id=room_id(light),
        room_name=str(getattr(getattr(light, "room", None), "name", "") or "Unassigned"),
        reachable=bool(getattr(light, "is_reachable", True)),
        capabilities=caps,
        on=bool(getattr(attrs, "is_on", False)),
        brightness=int(getattr(attrs, "light_level", 0) or 0),
        temperature=getattr(attrs, "color_temperature", None),
        temp_low=rng[0] if rng else None,
        temp_high=rng[1] if rng else None,
        color_mode=color_mode if color_mode in ("color", "temperature") else None,
        hue=float(getattr(attrs, "color_hue", 0) or 0),
        saturation=float(getattr(attrs, "color_saturation", 1) if getattr(attrs, "color_saturation", None) is not None else 1),
    )


def snapshot(hub: Any) -> list[LightInfo]:
    return sorted((light_info(d) for d in hub.get_lights()), key=lambda l: (l.room_name.casefold(), l.name.casefold()))


def approved_visible(lights: list[LightInfo], approved: dict[str, str]) -> list[LightInfo]:
    """Only show lamps with a persisted matching device ID AND original room ID."""
    return [l for l in lights if approved.get(l.id) == l.room_id]


def tray_power_targets(lights: list[LightInfo], approved: dict[str, str],
                       chosen_room: str | None = None) -> list[LightInfo]:
    """Return approved lights in the selected room or in all rooms."""
    visible = approved_visible(lights, approved)
    if chosen_room == ALL_APPROVED:
        return visible
    return [light for light in visible if light.room_id == chosen_room]


def tray_targets(lights: list[LightInfo], approved: dict[str, str],
                 chosen_room: str | None = None) -> list[LightInfo]:
    """Dim reachable approved lights that are already on in the tray selection."""
    return [light for light in tray_power_targets(lights, approved, chosen_room)
            if light.reachable and light.on and light.can("brightness")]


def stepped_brightness(values: list[int], delta_steps: int, step: int = 5) -> int:
    """One shared brightness for the room, based on its current mean."""
    if not values:
        raise ValueError("No active approved bulbs")
    start = round(sum(values) / len(values))
    return max(1, min(100, start + delta_steps * step))


def _validate(light: Any, action: str, value: Any) -> None:
    item = light_info(light)
    if not item.reachable:
        raise ValueError(f"{item.name}: bulb is offline")
    if not item.can(action):
        raise ValueError(f"{item.name}: {action} is unsupported")
    if action == "brightness" and (type(value) is not int or not 1 <= value <= 100):
        raise ValueError("Brightness must be between 1 and 100")
    if action == "power" and type(value) is not bool:
        raise ValueError("Power must be on/off")
    if action == "temperature" and (type(value) is not int or not item.temp_low <= value <= item.temp_high):
        raise ValueError(f"{item.name}: temperature outside supported {item.temp_low}–{item.temp_high} K")
    if action == "color":
        try:
            hue, saturation = value
            if not (0 <= float(hue) <= 360 and 0 <= float(saturation) <= 1):
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError("Color must be hue 0–360 and saturation 0–1") from None


def apply(hub: Any, approved: dict[str, str], target_ids: list[str], action: str, value: Any) -> int:
    """Preflight every target (including fresh room assignment) before changing any bulb.

    Important: this is an accident-prevention gate, not a security boundary against
    other programs that have the unrestricted DIRIGERA access token.
    """
    if action not in ACTIONS:
        raise ValueError("Unknown action")
    ids = list(dict.fromkeys(target_ids))
    if not ids:
        raise PermissionError("No explicitly approved lights were selected")
    for id_ in ids:
        if id_ not in approved:
            raise PermissionError(f"Light {id_} is not approved")

    lights = []
    for id_ in ids:
        light = hub.get_light_by_id(id_=id_)
        if room_id(light) != approved[id_]:
            raise PermissionError(f"{id_}: room changed since approval; re-approve before use")
        _validate(light, action, value)
        lights.append(light)

    for light in lights:
        if action == "power":
            light.set_light(lamp_on=value)
        elif action == "brightness":
            light.set_light_level(light_level=value)
        elif action == "temperature":
            light.set_color_temperature(color_temp=value)
        else:
            light.set_light_color(hue=float(value[0]), saturation=float(value[1]))
    return len(lights)


def config_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return root / "Lysnav" / "config.json"


def read_config(path: Path | None = None) -> dict:
    if path is None:
        path = config_path()
        if not path.exists():
            # Read the previous installation until Lysnav saves its first settings.
            path = path.parent.parent / "DIRIGERA" / path.name
    if not path.exists():
        return {"ip": "", "token": "", "approved": {}, "approved_sensors": {},
                "tray_room": None, "tray_icon_path": ""}
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("approved", {})
    approved = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    selected_room = data.get("tray_room", "")
    if selected_room == "":
        # Legacy Automatic selected a room only when the choice was unambiguous.
        rooms = set(approved.values())
        selected_room = next(iter(rooms)) if len(rooms) == 1 else None
    sensor_raw = data.get("approved_sensors", {})
    return {
        "ip": str(data.get("ip", "")),
        "token": str(data.get("token", "")),
        "approved": approved,
        "approved_sensors": {str(k): str(v) for k, v in sensor_raw.items()} if isinstance(sensor_raw, dict) else {},
        "tray_room": selected_room if isinstance(selected_room, str) else None,
        # Do not interpret an old KDE icon theme name as a Windows file path.
        "tray_icon_path": data.get("tray_icon_path", "") if isinstance(data.get("tray_icon_path"), str) else "",
    }


def write_config(config: dict, path: Path | None = None) -> None:
    if path is None:
        path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix="config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@dataclass(frozen=True)
class EnvironmentInfo:
    """Read-only data. Sensor approvals are separate from lighting permissions."""
    id: str
    name: str
    room_id: str
    room_name: str
    reachable: bool
    temperature: float | None
    humidity: float | None
    battery: int | None
    relation_id: str | None = None


def _numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def environment_info(sensor: Any) -> EnvironmentInfo:
    attrs = sensor.attributes
    battery = _numeric(getattr(attrs, "battery_percentage", None))
    return EnvironmentInfo(
        id=str(sensor.id),
        name=str(getattr(attrs, "custom_name", "") or "Unnamed climate sensor"),
        room_id=room_id(sensor),
        room_name=str(getattr(getattr(sensor, "room", None), "name", "") or "Unassigned"),
        reachable=bool(getattr(sensor, "is_reachable", True)),
        temperature=_numeric(getattr(attrs, "current_temperature", None)),
        humidity=_numeric(getattr(attrs, "current_r_h", None)),
        battery=int(battery) if battery is not None else None,
        relation_id=str(getattr(sensor, "relation_id", None) or "") or None,
    )


def environment_snapshot(hub: Any) -> list[EnvironmentInfo]:
    return sorted(
        (environment_info(sensor) for sensor in hub.get_environment_sensors()),
        key=lambda s: (s.room_name.casefold(), s.name.casefold(), s.id),
    )


def approved_climate(sensors: list[EnvironmentInfo], approved: dict[str, str]) -> list[EnvironmentInfo]:
    """Merge two sensor endpoints of ONE approved physical sensor, read-only.

    An approved and still-room-matched sensor record is required to anchor every
    group. Its unassigned companion may supply extra measurements, but ONLY when
    the hub supplies an identical nonempty relation_id. Companions assigned to
    any other room never join, and unrelated unapproved sensors are never shown.
    """
    # Prefer the physical device endpoint that has an actual assigned room.
    # Otherwise an approved unassigned humidity half could become the label.
    anchors = sorted(approved_visible(sensors, approved),
                     key=lambda s: (s.room_id == UNASSIGNED, s.name.casefold(), s.id))
    related: dict[str, list[EnvironmentInfo]] = {}
    for sensor in sensors:
        if sensor.relation_id:
            related.setdefault(sensor.relation_id, []).append(sensor)
    output: list[EnvironmentInfo] = []
    consumed: set[str] = set()
    for anchor in anchors:
        if anchor.id in consumed:
            continue
        members = [anchor]
        if anchor.relation_id:
            for peer in related.get(anchor.relation_id, []):
                if peer.id == anchor.id:
                    continue
                # Never show an unapproved peer assigned to another real room.
                # An approved peer still needs to match its originally saved room.
                if peer.room_id == UNASSIGNED and (peer.id not in approved or approved[peer.id] == UNASSIGNED):
                    members.append(peer)
                elif peer.id in approved and approved[peer.id] == peer.room_id and peer.room_id == anchor.room_id:
                    members.append(peer)
        consumed.update(s.id for s in members)
        # Always use anchor's approved room, name and ID for display.
        # Pick a record with actual data, not a None from another endpoint.
        output.append(EnvironmentInfo(
            id=anchor.id,
            name=anchor.name,
            room_id=anchor.room_id,
            room_name=anchor.room_name,
            reachable=any(s.reachable for s in members),
            temperature=next((s.temperature for s in members if s.reachable and s.temperature is not None), None),
            humidity=next((s.humidity for s in members if s.reachable and s.humidity is not None), None),
            battery=next((s.battery for s in members if s.reachable and s.battery is not None), None),
            relation_id=anchor.relation_id,
        ))
    return output


def toggle_power(hub: Any, approved: dict[str, str], target_ids: list[str],
                 *, all_rooms: bool = False) -> tuple[int, bool]:
    """Toggle approved devices only: any ON => all OFF, otherwise all ON.

    Re-fetch state and validate EVERY device/room before the first write.
    """
    ids = list(dict.fromkeys(target_ids))
    if not ids:
        raise PermissionError('No approved lights selected')
    fresh = []
    for ident in ids:
        if ident not in approved:
            raise PermissionError(f'Light {ident} is not approved')
        light = hub.get_light_by_id(id_=ident)
        if room_id(light) != approved[ident]:
            raise PermissionError(f'{ident}: room changed since approval')
        _validate(light, 'power', True)
        fresh.append(light)
    if not all_rooms and len({room_id(light) for light in fresh}) != 1:
        raise PermissionError('Cannot toggle more than one room')
    turn_on = not any(bool(getattr(light.attributes, 'is_on', False)) for light in fresh)
    for light in fresh:
        light.set_light(lamp_on=turn_on)
    return len(fresh), turn_on


def wheel_steps(delta: int, remainder: int = 0) -> tuple[int, int]:
    """Accumulate high-resolution Windows input to one step per 120 units."""
    total = remainder + delta
    steps = math.trunc(total / 120)
    return steps, total - steps * 120
