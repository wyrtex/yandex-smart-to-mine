"""Модели ответа Yandex Smart Home IoT API (https://api.iot.yandex.net).

Все модели пропускают неизвестные поля (extra="allow"), чтобы не терять
ничего из того, что отдаёт Яндекс, даже если формат API расширится.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


class State(_Base):
    instance: str | None = None
    value: Any = None


class Capability(_Base):
    """Умение устройства: то, чем можно управлять (вкл/выкл, яркость, цвет...)."""

    type: str  # например devices.capabilities.on_off
    retrievable: bool = True
    reportable: bool = False
    parameters: dict[str, Any] = Field(default_factory=dict)
    state: State | None = None
    last_updated: float | None = None

    @property
    def short_type(self) -> str:
        return self.type.removeprefix("devices.capabilities.")

    @property
    def instance(self) -> str | None:
        """Имя instance: из параметров или из состояния."""
        if self.parameters.get("instance"):
            return self.parameters["instance"]
        if self.short_type == "on_off":
            return "on"
        if self.state and self.state.instance:
            return self.state.instance
        return None


class Property(_Base):
    """Свойство устройства: то, что оно сообщает (температура, открытие...)."""

    type: str  # devices.properties.float | devices.properties.event
    retrievable: bool = True
    reportable: bool = False
    parameters: dict[str, Any] = Field(default_factory=dict)
    state: State | None = None
    last_updated: float | None = None

    @property
    def short_type(self) -> str:
        return self.type.removeprefix("devices.properties.")

    @property
    def instance(self) -> str | None:
        return self.parameters.get("instance") or (self.state.instance if self.state else None)

    @property
    def unit(self) -> str | None:
        unit = self.parameters.get("unit")
        return unit.removeprefix("unit.") if unit else None


class Device(_Base):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    type: str  # например devices.types.light
    external_id: str | None = None
    skill_id: str | None = None
    household_id: str | None = None
    room: str | None = None
    groups: list[str] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    properties: list[Property] = Field(default_factory=list)

    @property
    def short_type(self) -> str:
        return self.type.removeprefix("devices.types.")

    def capability(self, short_type: str, instance: str | None = None) -> Capability | None:
        for cap in self.capabilities:
            if cap.short_type == short_type and (instance is None or cap.instance == instance):
                return cap
        return None

    def property(self, instance: str) -> Property | None:
        for prop in self.properties:
            if prop.instance == instance:
                return prop
        return None


class Room(_Base):
    id: str
    name: str
    household_id: str | None = None
    devices: list[str] = Field(default_factory=list)


class Group(_Base):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    type: str | None = None
    household_id: str | None = None
    devices: list[str] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)


class Scenario(_Base):
    id: str
    name: str
    is_active: bool = True


class Household(_Base):
    id: str
    name: str
    type: str | None = None


class UserInfo(_Base):
    status: str
    request_id: str | None = None
    rooms: list[Room] = Field(default_factory=list)
    groups: list[Group] = Field(default_factory=list)
    devices: list[Device] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    households: list[Household] = Field(default_factory=list)

    def room_name(self, room_id: str | None) -> str | None:
        return next((r.name for r in self.rooms if r.id == room_id), None)

    def device(self, id_or_name: str) -> Device | None:
        """Поиск устройства по id или по имени (без учёта регистра)."""
        needle = id_or_name.casefold()
        for dev in self.devices:
            if dev.id == id_or_name or dev.name.casefold() == needle:
                return dev
        return None


class ActionResult(_Base):
    status: str
    request_id: str | None = None
    devices: list[dict[str, Any]] = Field(default_factory=list)
