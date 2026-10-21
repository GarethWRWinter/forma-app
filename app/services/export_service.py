"""
Workout and ride export builders.

Supports:
- ZWO (Zwift Workout) - XML format for Zwift structured workouts
- ERG and MRC - minutes/watts and minutes/percent files for trainer apps
- FIT (Flexible and Interoperable Data Transfer) - binary format for Garmin/Wahoo
- GPX (GPS Exchange Format) - XML format for GPS tracks

Every workout file is written in the order ride mode rides it (one shared
flattening, the same as useTrainingSession.flattenSteps): an interval_on and
the interval_off after it repeat as on, off, on, off. And none of them ever
asks a trainer to hold more than ERG_CAP (1.30 x FTP) in ERG. A step above
it is a max effort: in ZWO a FreeRide block and in FIT an open target, so
the trainer lets go; ERG and MRC have no way to let go, so the target is
held at the cap and the course text tells the rider to switch ERG off.
"""

import struct
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.dom import minidom

from sqlalchemy.orm import Session

from app.models.ride import RideData
from app.models.training import Workout, WorkoutStep
from app.services.safety_service import ERG_CAP

_EPS = 1e-6
_RAMPS = ("warmup", "cooldown", "ramp")

# What the rider sees at a max-effort step. British English, no dashes.
MAX_EFFORT_ZWO = "Max effort. ERG is off for this one, so ride it all out."
MAX_EFFORT_COURSE_TEXT = f"Max effort: ERG off, ride it all out (this file holds {ERG_CAP:.0%} of FTP)"
# Shown this long before a max effort, so there's time to switch ERG off.
MAX_EFFORT_WARNING_SECONDS = 30
MAX_EFFORT_WARNING = "Max effort coming up: switch ERG off now"
MAX_EFFORT_FIT_NAME = "Max effort"


def _step_type(step) -> str:
    value = step.step_type
    return getattr(value, "value", value)


def flatten_steps(workout: Workout) -> list[WorkoutStep]:
    """Every step in the order it is ridden, exactly as ride mode flattens
    them: an interval_on followed by an interval_off repeats as on, off, on,
    off, repeat_count times; an interval_on alone repeats on its own; every
    other step is ridden once."""
    steps = sorted(workout.steps, key=lambda s: s.step_order)
    flat: list[WorkoutStep] = []
    i = 0
    while i < len(steps):
        step = steps[i]
        if _step_type(step) == "interval_on":
            nxt = steps[i + 1] if i + 1 < len(steps) else None
            off = nxt if nxt is not None and _step_type(nxt) == "interval_off" else None
            for _ in range(max(step.repeat_count or 1, 1)):
                flat.append(step)
                if off is not None:
                    flat.append(off)
            if off is not None:
                i += 1
        else:
            flat.append(step)
        i += 1
    return flat


def _over_cap(pct: float) -> bool:
    return pct > ERG_CAP + _EPS


def _capped(pct: float) -> float:
    return min(pct, ERG_CAP)


def _steady_pct(step, default: float) -> float:
    return step.power_target_pct or default


def _ramp_pcts(step) -> tuple[float, float]:
    """A ramp's start and end, as ERG, MRC and FIT write them, held to the
    cap: a ramp that crosses ERG_CAP is held up to it, never beyond."""
    low = step.power_low_pct or 0.40
    high = step.power_high_pct or step.power_target_pct or 0.70
    return _capped(low), _capped(high)


def _step_label(step) -> str:
    return step.notes or _step_type(step).replace("_", " ").title()


# === ZWO Export (Zwift Workout Format) ===


def _zwo_max_effort(wo, duration: int) -> None:
    """A FreeRide block: Zwift lets go of ERG for it, so the rider sprints
    against their own resistance instead of a trainer holding 200%."""
    elem = ET.SubElement(wo, "FreeRide")
    elem.set("Duration", str(duration))
    text = ET.SubElement(elem, "textevent")
    text.set("timeoffset", "0")
    text.set("message", MAX_EFFORT_ZWO)


def _zwo_single(wo, step) -> None:
    """One ridden step, on its own."""
    st = _step_type(step)

    if st == "warmup":
        elem = ET.SubElement(wo, "Warmup")
        elem.set("Duration", str(step.duration_seconds))
        low = step.power_low_pct or (step.power_target_pct * 0.7 if step.power_target_pct else 0.40)
        high = step.power_high_pct or step.power_target_pct or 0.65
        elem.set("PowerLow", f"{_capped(low):.2f}")
        elem.set("PowerHigh", f"{_capped(high):.2f}")
        if step.cadence_target:
            elem.set("Cadence", str(step.cadence_target))

    elif st == "cooldown":
        elem = ET.SubElement(wo, "Cooldown")
        elem.set("Duration", str(step.duration_seconds))
        high = step.power_high_pct or step.power_target_pct or 0.55
        low = step.power_low_pct or (step.power_target_pct * 0.7 if step.power_target_pct else 0.35)
        elem.set("PowerLow", f"{_capped(low):.2f}")
        elem.set("PowerHigh", f"{_capped(high):.2f}")

    elif st == "ramp":
        elem = ET.SubElement(wo, "Warmup")
        elem.set("Duration", str(step.duration_seconds))
        elem.set("PowerLow", f"{_capped(step.power_low_pct or 0.40):.2f}")
        elem.set("PowerHigh", f"{_capped(step.power_high_pct or 1.00):.2f}")

    elif st == "free_ride":
        elem = ET.SubElement(wo, "FreeRide")
        elem.set("Duration", str(step.duration_seconds))

    else:
        # steady_state, interval_on, interval_off: one held target.
        default = {"interval_on": 1.00, "interval_off": 0.50}.get(st, 0.65)
        pct = _steady_pct(step, default)
        if _over_cap(pct):
            _zwo_max_effort(wo, step.duration_seconds)
            return
        elem = ET.SubElement(wo, "SteadyState")
        elem.set("Duration", str(step.duration_seconds))
        elem.set("Power", f"{pct:.2f}")
        if step.cadence_target:
            elem.set("Cadence", str(step.cadence_target))


def workout_to_zwo(workout: Workout, ftp: int = 200) -> str:
    """
    Convert a workout with steps to Zwift ZWO format.

    ZWO uses power as a decimal fraction of FTP (e.g., 0.75 = 75% FTP).
    Steps are mapped to ZWO elements:
        warmup -> <Warmup>
        cooldown -> <Cooldown>
        steady_state -> <SteadyState>
        interval_on/interval_off -> <IntervalsT> (Zwift alternates on and
            off itself), unless either half is above ERG_CAP: then each
            repeat is written out, the hard half as a <FreeRide> max effort
        ramp -> <Warmup> with different PowerLow/PowerHigh
        free_ride -> <FreeRide>
    """
    root = ET.Element("workout_file")

    # Header
    ET.SubElement(root, "author").text = "Gareth Coaching"
    ET.SubElement(root, "name").text = workout.title
    ET.SubElement(root, "description").text = workout.description or ""
    ET.SubElement(root, "sportType").text = "bike"

    # Tags
    tags = ET.SubElement(root, "tags")
    if workout.workout_type:
        tag = ET.SubElement(tags, "tag")
        tag.set("name", getattr(workout.workout_type, "value", workout.workout_type))

    # Workout
    wo = ET.SubElement(root, "workout")

    steps = sorted(workout.steps, key=lambda s: s.step_order)
    i = 0
    while i < len(steps):
        step = steps[i]
        if _step_type(step) == "interval_on":
            nxt = steps[i + 1] if i + 1 < len(steps) else None
            off = nxt if nxt is not None and _step_type(nxt) == "interval_off" else None
            repeats = max(step.repeat_count or 1, 1)
            on_pct = _steady_pct(step, 1.00)
            off_pct = _steady_pct(off, 0.50) if off is not None else None

            if off is not None and not _over_cap(on_pct) and not _over_cap(off_pct):
                elem = ET.SubElement(wo, "IntervalsT")
                elem.set("Repeat", str(repeats))
                elem.set("OnDuration", str(step.duration_seconds))
                elem.set("OffDuration", str(off.duration_seconds))
                elem.set("OnPower", f"{on_pct:.2f}")
                elem.set("OffPower", f"{off_pct:.2f}")
                if step.cadence_target:
                    elem.set("CadenceResting", str(off.cadence_target or 80))
                    elem.set("Cadence", str(step.cadence_target))
            else:
                # Written out repeat by repeat, in the order they're ridden.
                for _ in range(repeats):
                    _zwo_single(wo, step)
                    if off is not None:
                        _zwo_single(wo, off)
            if off is not None:
                i += 1
        else:
            _zwo_single(wo, step)
        i += 1

    # Pretty print
    xml_str = ET.tostring(root, encoding="unicode")
    dom = minidom.parseString(xml_str)
    return dom.toprettyxml(indent="  ", encoding=None)


# === ERG and MRC Export ===


def _course_points(workout: Workout) -> tuple[list[tuple[float, float]], list[tuple[int, str]]]:
    """The minutes/fraction-of-FTP points both ERG and MRC files carry, and
    the course text for each step, in ridden order. A step above ERG_CAP is
    held at the cap (these formats can't let go) and its text says so."""
    points: list[tuple[float, float]] = []
    texts: list[tuple[int, str]] = []
    minutes = 0.0
    elapsed = 0
    for step in flatten_steps(workout):
        st = _step_type(step)
        label = _step_label(step)
        if st in _RAMPS:
            start, finish = _ramp_pcts(step)
        elif st == "free_ride":
            start = finish = 0.50
        else:
            pct = _steady_pct(step, 0.65)
            if _over_cap(pct):
                label = MAX_EFFORT_COURSE_TEXT
                # A heads-up during the step before, in time order.
                warn_at = max(elapsed - MAX_EFFORT_WARNING_SECONDS, texts[-1][0] + 1 if texts else 0)
                if warn_at < elapsed:
                    texts.append((warn_at, MAX_EFFORT_WARNING))
            start = finish = _capped(pct)
        points.append((minutes, start))
        minutes += step.duration_seconds / 60.0
        points.append((minutes, finish))
        texts.append((elapsed, label))
        elapsed += step.duration_seconds
    return points, texts


def workout_to_erg(workout: Workout, ftp: int = 200) -> str:
    """
    Convert a workout to ERG format (absolute watts).

    ERG files use minutes/watts pairs with linear interpolation between points.
    Used by TrainerRoad, Wahoo KICKR, and other smart trainers.
    """
    lines = [
        "[COURSE HEADER]",
        "VERSION = 2",
        "UNITS = ENGLISH",
        f"DESCRIPTION = {workout.description or workout.title}",
        f"FILE NAME = {workout.title.replace(' ', '_')}.erg",
        f"FTP = {ftp}",
        "MINUTES WATTS",
        "[END COURSE HEADER]",
        "[COURSE DATA]",
    ]
    points, texts = _course_points(workout)
    for minute, pct in points:
        lines.append(f"{minute:.2f}\t{round(pct * ftp)}")
    lines.append("[END COURSE DATA]")

    # Course text for step labels
    lines.append("[COURSE TEXT]")
    for second, label in texts:
        lines.append(f"{second}\t{label}\t10")
    lines.append("[END COURSE TEXT]")

    return "\n".join(lines)


def workout_to_mrc(workout: Workout, ftp: int = 200) -> str:
    """
    Convert a workout to MRC format (% FTP).

    MRC files use minutes/percent pairs with linear interpolation.
    Used by TrainerRoad, Wahoo, and other smart trainer apps.
    """
    lines = [
        "[COURSE HEADER]",
        "VERSION = 2",
        "UNITS = ENGLISH",
        f"DESCRIPTION = {workout.description or workout.title}",
        f"FILE NAME = {workout.title.replace(' ', '_')}.mrc",
        "MINUTES PERCENT",
        "[END COURSE HEADER]",
        "[COURSE DATA]",
    ]
    points, texts = _course_points(workout)
    for minute, pct in points:
        lines.append(f"{minute:.2f}\t{pct * 100:.0f}")
    lines.append("[END COURSE DATA]")

    lines.append("[COURSE TEXT]")
    for second, label in texts:
        lines.append(f"{second}\t{label}\t10")
    lines.append("[END COURSE TEXT]")

    return "\n".join(lines)


# === FIT Workout Export (Binary) ===


def _fit_string(text: str, size: int = 16) -> bytes:
    """A fixed-size FIT string field: UTF-8, cut on a character boundary so
    a multi-byte character can't overflow the field, null-padded."""
    raw = text.encode("utf-8")[: size - 1]
    raw = raw.decode("utf-8", errors="ignore").encode("utf-8")
    return raw + b"\x00" * (size - len(raw))


def workout_to_fit(workout: Workout, ftp: int = 200) -> bytes:
    """
    Convert a workout to Garmin FIT workout format.

    FIT is a binary format used by Garmin, Wahoo, and Hammerhead devices.
    This builds a minimal valid FIT file with workout and workout_step messages.
    A head unit driving a trainer sets ERG from each step's power target, so
    a step above ERG_CAP (and a free ride) gets an open target: no ERG.
    """
    # FIT message types
    MESG_FILE_ID = 0
    MESG_WORKOUT = 26
    MESG_WORKOUT_STEP = 27

    # Intensity types
    INTENSITY_ACTIVE = 0
    INTENSITY_REST = 1
    INTENSITY_WARMUP = 2
    INTENSITY_COOLDOWN = 3

    # Duration/target types
    DURATION_TIME = 0
    TARGET_OPEN = 2
    TARGET_POWER = 4

    # Step type mapping
    def _step_intensity(st: str) -> int:
        if st == "warmup":
            return INTENSITY_WARMUP
        elif st == "cooldown":
            return INTENSITY_COOLDOWN
        elif st in ("interval_off", "free_ride"):
            return INTENSITY_REST
        else:
            return INTENSITY_ACTIVE

    flat_steps = flatten_steps(workout)

    # Build FIT data records as raw bytes
    # We'll build a simple FIT file with Definition + Data messages
    records = bytearray()

    # --- File ID Message (Definition + Data) ---
    # Definition Message: record header(1) + reserved(1) + arch(1) + global mesg(2) + num fields(1) + field defs(3*n)
    file_id_fields = [
        (0, 1, 0),   # type: enum (1 byte) - 0=file
        (1, 2, 132), # manufacturer: uint16 - 1=garmin
        (2, 2, 132), # product: uint16
        (3, 4, 134), # serial_number: uint32z
        (4, 4, 134), # time_created: uint32
    ]
    records += _fit_definition(0, MESG_FILE_ID, file_id_fields)
    # Data: type=5 (workout), manufacturer=1, product=1, serial=12345, time=1000000000
    records += _fit_data_record(0, struct.pack("<BHHII", 5, 1, 1, 12345, 1000000000))

    # --- Workout Message ---
    wo_fields = [
        (4, 1, 0),    # sport: enum - 2=cycling
        (8, 2, 132),  # num_valid_steps: uint16
        (0, 16, 7),   # wkt_name: string (16 bytes)
    ]
    records += _fit_definition(1, MESG_WORKOUT, wo_fields)
    records += _fit_data_record(
        1, struct.pack("<BH", 2, len(flat_steps)) + _fit_string(workout.title or "Workout")
    )

    # --- Workout Step Messages ---
    ws_fields = [
        (0, 16, 7),   # wkt_step_name: string
        (1, 1, 0),    # duration_type: enum
        (2, 4, 134),  # duration_value: uint32
        (3, 1, 0),    # target_type: enum
        (4, 4, 134),  # target_value: uint32
        (5, 4, 134),  # custom_target_value_low: uint32
        (6, 4, 134),  # custom_target_value_high: uint32
        (7, 1, 0),    # intensity: enum
        (254, 2, 132),# message_index: uint16
    ]
    records += _fit_definition(2, MESG_WORKOUT_STEP, ws_fields)

    for idx, step in enumerate(flat_steps):
        st = _step_type(step)
        name = step.notes or st.replace("_", " ")
        duration_ms = step.duration_seconds * 1000
        intensity = _step_intensity(st)
        target_type = TARGET_POWER

        # Power targets: FIT uses watts + 1000 offset for custom targets
        if st in _RAMPS:
            low_pct, high_pct = _ramp_pcts(step)
            low_w = round(low_pct * ftp) + 1000
            high_w = round(high_pct * ftp) + 1000
        elif st == "free_ride":
            target_type, low_w, high_w = TARGET_OPEN, 0, 0
        else:
            pct = _steady_pct(step, 0.65)
            if _over_cap(pct):
                # A max effort: no power target, so nothing for ERG to hold.
                target_type, low_w, high_w = TARGET_OPEN, 0, 0
                name = MAX_EFFORT_FIT_NAME
            else:
                target_w = round(pct * ftp)
                # ±5W range
                low_w = target_w - 5 + 1000
                high_w = target_w + 5 + 1000

        data = _fit_string(name) + struct.pack(
            "<BIBIIIBH",
            DURATION_TIME,     # duration_type
            duration_ms,       # duration_value
            target_type,       # target_type
            0,                 # target_value (0 = custom, or open)
            low_w,             # custom_target_value_low
            high_w,            # custom_target_value_high
            intensity,         # intensity
            idx,               # message_index
        )
        records += _fit_data_record(2, data)

    # Build complete FIT file
    return _build_fit_file(bytes(records))


def _fit_definition(local_mesg: int, global_mesg: int, fields: list) -> bytes:
    """Build a FIT definition message."""
    header = 0x40 | (local_mesg & 0x0F)  # Definition message flag
    result = struct.pack("<BBBHB", header, 0, 0, global_mesg, len(fields))
    for field_def_num, size, base_type in fields:
        result += struct.pack("<BBB", field_def_num, size, base_type)
    return result


def _fit_data_record(local_mesg: int, data: bytes) -> bytes:
    """Build a FIT data message."""
    header = bytes([local_mesg & 0x0F])
    return header + data


def _build_fit_file(records: bytes) -> bytes:
    """Wrap records in a FIT file with header and CRC."""

    data_size = len(records)

    # 14-byte header
    header = struct.pack(
        "<BBHI4s",
        14,             # header size
        0x20,           # protocol version 2.0
        0x0811,         # profile version 21.17
        data_size,      # data size
        b".FIT",        # data type
    )
    header_crc = _fit_crc(header[:12])
    header += struct.pack("<H", header_crc)

    # File CRC over header + data
    file_crc = _fit_crc(header + records)

    return header + records + struct.pack("<H", file_crc)


def _fit_crc(data: bytes) -> int:
    """Calculate FIT file CRC-16."""
    crc_table = [
        0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
        0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
    ]
    crc = 0
    for byte in data:
        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ crc_table[byte & 0xF]
        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ crc_table[(byte >> 4) & 0xF]
    return crc


# === GPX Export (GPS Exchange Format) ===

def ride_to_gpx(
    db: Session, ride_id: str, ride_title: str = "Ride"
) -> str:
    """
    Export ride GPS data to GPX format.

    Only includes data points that have latitude/longitude.
    """
    rows = (
        db.query(RideData)
        .filter(
            RideData.ride_id == ride_id,
            RideData.latitude.isnot(None),
            RideData.longitude.isnot(None),
        )
        .order_by(RideData.elapsed_seconds)
        .all()
    )

    if not rows:
        return _empty_gpx(ride_title)

    # GPX XML structure
    gpx = ET.Element("gpx")
    gpx.set("version", "1.1")
    gpx.set("creator", "Gareth Coaching")
    gpx.set("xmlns", "http://www.topografix.com/GPX/1/1")
    gpx.set("xmlns:gpxtpx", "http://www.garmin.com/xmlschemas/TrackPointExtension/v1")

    metadata = ET.SubElement(gpx, "metadata")
    ET.SubElement(metadata, "name").text = ride_title

    trk = ET.SubElement(gpx, "trk")
    ET.SubElement(trk, "name").text = ride_title
    trkseg = ET.SubElement(trk, "trkseg")

    for row in rows:
        trkpt = ET.SubElement(trkseg, "trkpt")
        trkpt.set("lat", f"{row.latitude:.7f}")
        trkpt.set("lon", f"{row.longitude:.7f}")

        if row.altitude is not None:
            ET.SubElement(trkpt, "ele").text = f"{row.altitude:.1f}"

        if row.timestamp:
            ts = row.timestamp
            if isinstance(ts, datetime):
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                ET.SubElement(trkpt, "time").text = ts.isoformat()

        # Extensions for power, HR, cadence
        extensions = ET.SubElement(trkpt, "extensions")
        tpx = ET.SubElement(extensions, "gpxtpx:TrackPointExtension")
        if row.heart_rate is not None:
            ET.SubElement(tpx, "gpxtpx:hr").text = str(row.heart_rate)
        if row.cadence is not None:
            ET.SubElement(tpx, "gpxtpx:cad").text = str(row.cadence)
        if row.power is not None:
            ET.SubElement(tpx, "gpxtpx:power").text = str(row.power)

    xml_str = ET.tostring(gpx, encoding="unicode")
    dom = minidom.parseString(xml_str)
    return dom.toprettyxml(indent="  ", encoding=None)


def _empty_gpx(title: str) -> str:
    """Return an empty GPX file."""
    gpx = ET.Element("gpx")
    gpx.set("version", "1.1")
    gpx.set("creator", "Gareth Coaching")
    gpx.set("xmlns", "http://www.topografix.com/GPX/1/1")
    metadata = ET.SubElement(gpx, "metadata")
    ET.SubElement(metadata, "name").text = title
    xml_str = ET.tostring(gpx, encoding="unicode")
    dom = minidom.parseString(xml_str)
    return dom.toprettyxml(indent="  ", encoding=None)
