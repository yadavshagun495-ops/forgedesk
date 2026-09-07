"""Synthetic booking system for "Forge Auto Care" (no real customer data).

All writes are fenced: a mutation commits only if the epoch that requested it is still the
current conversational epoch, otherwise it raises StaleEpoch and nothing changes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
from dataclasses import asdict, dataclass, field

from ..fence import FenceToken

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PERIODS = {
    "morning": (9, 12),
    "afternoon": (12, 17),
    "evening": (17, 19),
    "any": (9, 19),
}
SERVICES = ["oil change", "tire rotation", "brake inspection", "general service", "battery check"]


@dataclass
class Slot:
    slot_id: str
    date: str  # ISO date
    day: str  # e.g. "Thursday"
    hour: int
    available: bool = True

    @property
    def time_label(self) -> str:
        h12 = self.hour % 12 or 12
        return f"{h12} {'AM' if self.hour < 12 else 'PM'}"

    def public(self) -> dict:
        return {"slot_id": self.slot_id, "date": self.date, "day": self.day, "time": self.time_label}


@dataclass
class Appointment:
    code: str
    slot: Slot
    customer_name: str
    vehicle: str
    service: str
    created_epoch: int
    reported_to_user: bool = False
    cancelled: bool = False

    def public(self) -> dict:
        return {
            "code": self.code,
            "date": self.slot.date,
            "day": self.slot.day,
            "time": self.slot.time_label,
            "customer_name": self.customer_name,
            "vehicle": self.vehicle,
            "service": self.service,
            "cancelled": self.cancelled,
        }


class BookingError(Exception):
    pass


@dataclass
class BookingStore:
    today: dt.date = field(default_factory=dt.date.today)
    seed: int = 7
    days_ahead: int = 8
    slots: dict[str, Slot] = field(default_factory=dict)
    appointments: dict[str, Appointment] = field(default_factory=dict)
    commits: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        rnd = random.Random(self.seed)
        for i in range(1, self.days_ahead + 1):
            date = self.today + dt.timedelta(days=i)
            if date.weekday() == 6:  # closed Sundays
                continue
            for hour in range(9, 19):
                sid = f"{date.isoformat()}T{hour:02d}"
                taken = rnd.random() < 0.45
                self.slots[sid] = Slot(sid, date.isoformat(), date.strftime("%A"), hour, available=not taken)
        self._lock = asyncio.Lock()
        self._code_rnd = random.Random(self.seed + 1)

    # ------------------------------------------------------------------ helpers
    def resolve_day(self, day: str) -> dt.date | None:
        d = day.strip().lower()
        if d in ("today",):
            return self.today
        if d in ("tomorrow",):
            return self.today + dt.timedelta(days=1)
        for i, name in enumerate(DAYS):
            if d.startswith(name[:3]):
                delta = (i - self.today.weekday()) % 7
                if delta == 0:
                    delta = 7  # "Tuesday" said on a Tuesday means next week
                return self.today + dt.timedelta(days=delta)
        try:
            return dt.date.fromisoformat(d)
        except ValueError:
            return None

    def _new_code(self) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to keep it speakable
        while True:
            code = "FD-" + "".join(self._code_rnd.choice(alphabet) for _ in range(4))
            if code not in self.appointments:
                return code

    # ------------------------------------------------------------------ read tools
    async def check_availability(self, day: str, period: str = "any") -> dict:
        date = self.resolve_day(day)
        if date is None:
            raise BookingError(f"could not understand the day {day!r}")
        lo, hi = PERIODS.get(period.lower(), PERIODS["any"])
        free = [
            s for s in self.slots.values()
            if s.date == date.isoformat() and s.available and lo <= s.hour < hi
        ]
        free.sort(key=lambda s: s.hour)
        return {
            "day": date.strftime("%A"),
            "date": date.isoformat(),
            "period": period,
            "slots": [s.public() for s in free[:3]],
            "total_free": len(free),
        }

    async def lookup_appointment(self, code: str) -> dict:
        appt = self.appointments.get(code.upper().replace(" ", ""))
        if not appt:
            raise BookingError(f"no appointment with code {code}")
        return appt.public()

    # ------------------------------------------------------------------ mutations (fenced)
    async def book_appointment(
        self, slot_id: str, customer_name: str, vehicle: str, service: str, *, token: FenceToken
    ) -> dict:
        async with self._lock:
            token.check("book_appointment commit")
            slot = self.slots.get(slot_id)
            if not slot:
                raise BookingError(f"unknown slot {slot_id}")
            if not slot.available:
                raise BookingError(f"{slot.day} {slot.time_label} was just taken")
            slot.available = False
            appt = Appointment(self._new_code(), slot, customer_name, vehicle, service, token.epoch)
            self.appointments[appt.code] = appt
            self.commits.append({"op": "book", "code": appt.code, "epoch": token.epoch})
            return appt.public()

    async def reschedule_appointment(self, code: str, slot_id: str, *, token: FenceToken) -> dict:
        async with self._lock:
            token.check("reschedule_appointment commit")
            appt = self.appointments.get(code.upper().replace(" ", ""))
            if not appt or appt.cancelled:
                raise BookingError(f"no active appointment with code {code}")
            slot = self.slots.get(slot_id)
            if not slot or not slot.available:
                raise BookingError("that slot is not available")
            appt.slot.available = True
            slot.available = False
            appt.slot = slot
            appt.reported_to_user = False
            self.commits.append({"op": "reschedule", "code": appt.code, "epoch": token.epoch})
            return appt.public()

    async def cancel_appointment(self, code: str, *, token: FenceToken) -> dict:
        async with self._lock:
            token.check("cancel_appointment commit")
            appt = self.appointments.get(code.upper().replace(" ", ""))
            if not appt or appt.cancelled:
                raise BookingError(f"no active appointment with code {code}")
            appt.cancelled = True
            appt.slot.available = True
            self.commits.append({"op": "cancel", "code": appt.code, "epoch": token.epoch})
            return {"cancelled": True, **appt.public()}

    def snapshot(self) -> dict:
        return {
            "appointments": [a.public() for a in self.appointments.values()],
            "commits": list(self.commits),
        }


TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Look up open service slots for a day. Slow backend (may take seconds).",
            "parameters": {
                "type": "object",
                "properties": {
                    "day": {"type": "string", "description": "Weekday name, 'today', 'tomorrow', or ISO date"},
                    "period": {"type": "string", "enum": ["morning", "afternoon", "evening", "any"]},
                },
                "required": ["day"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Book a specific slot_id returned by check_availability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slot_id": {"type": "string"},
                    "customer_name": {"type": "string"},
                    "vehicle": {"type": "string"},
                    "service": {"type": "string"},
                },
                "required": ["slot_id", "customer_name", "vehicle", "service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": "Move an existing appointment (by confirmation code) to a new slot_id.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}, "slot_id": {"type": "string"}},
                "required": ["code", "slot_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": "Cancel an appointment by confirmation code.",
            "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_appointment",
            "description": "Fetch an appointment by confirmation code.",
            "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
        },
    },
]

MUTATIONS = {"book_appointment", "reschedule_appointment", "cancel_appointment"}
READ_ONLY = {"check_availability", "lookup_appointment"}


def slot_dict(slot: Slot) -> dict:
    return asdict(slot)
