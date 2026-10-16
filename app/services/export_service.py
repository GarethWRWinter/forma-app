"""
Workout and ride export builders.

Supports:
- ZWO (Zwift Workout) - XML format for Zwift structured workouts
- FIT (Flexible and Interoperable Data Transfer) - binary format for Garmin/Wahoo
- GPX (GPS Exchange Format) - XML format for GPS tracks
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from io import BytesIO
from xml.dom import minidom

from sqlalchemy.orm import Session

from app.models.ride import RideData
from app.models.training import Workout, WorkoutStep
from app.services.plan_service import ride_order


# === ZWO Export (Zwift Workout Format) ===

def workout_to_zwo(workout: Workout, ftp: int = 200) -> str:
    """
    Convert a workout with steps to Zwift ZWO format.

    ZWO uses power as a decimal fraction of FTP (e.g., 0.75 = 75% FTP).
    Steps are mapped to ZWO elements:
        warmup -> <Warmup>
        cooldown -> <Cooldown>
        steady_state -> <SteadyState>
        interval_on/interval_off -> <IntervalsT>
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
        tag.set("name", workout.workout_type)

    # Workout
    wo = ET.SubElement(root, "workout")

    steps = sorted(workout.steps, key=lambda s: s.step_order)
    i = 0
    while i < len(steps):
        step = steps[i]
        st = step.step_type

        if st == "warmup":
            elem = ET.SubElement(wo, "Warmup")
            elem.set("Duration", str(step.duration_seconds))
            low = step.power_low_pct or (step.power_target_pct * 0.7 if step.power_target_pct else 0.40)
            high = step.power_high_pct or step.power_target_pct or 0.65
            elem.set("PowerLow", f"{low:.2f}")
            elem.set("PowerHigh", f"{high:.2f}")
            if step.cadence_target:
                elem.set("Cadence", str(step.cadence_target))

        elif st == "cooldown":
            elem = ET.SubElement(wo, "Cooldown")
            elem.set("Duration", str(step.duration_seconds))
            high = step.power_high_pct or step.power_target_pct or 0.55
            low = step.power_low_pct or (step.power_target_pct * 0.7 if step.power_target_pct else 0.35)
            elem.set("PowerLow", f"{low:.2f}")
            elem.set("PowerHigh", f"{high:.2f}")

        elif st == "steady_state":
            elem = ET.SubElement(wo, "SteadyState")
            elem.set("Duration", str(step.duration_seconds))
            elem.set("Power", f"{step.power_target_pct:.2f}" if step.power_target_pct else "0.65")
            if step.cadence_target:
                elem.set("Cadence", str(step.cadence_target))

        elif st == "interval_on":
            # Look for the matching interval_off that follows
            off_step = steps[i + 1] if i + 1 < len(steps) and steps[i + 1].step_type == "interval_off" else None

            if off_step:
                elem = ET.SubElement(wo, "IntervalsT")
                elem.set("Repeat", str(step.repeat_count or 1))
                elem.set("OnDuration", str(step.duration_seconds))
                elem.set("OffDuration", str(off_step.duration_seconds))
                elem.set("OnPower", f"{step.power_target_pct:.2f}" if step.power_target_pct else "1.00")
                elem.set("OffPower", f"{off_step.power_target_pct:.2f}" if off_step.power_target_pct else "0.50")
                if step.cadence_target:
                    elem.set("CadenceResting", str(off_step.cadence_target or 80))
                    elem.set("Cadence", str(step.cadence_target))
                i += 1  # skip the off step
            else:
                # No matching off, treat as steady state
                elem = ET.SubElement(wo, "SteadyState")
                elem.set("Duration", str(step.duration_seconds))
                elem.set("Power", f"{step.power_target_pct:.2f}" if step.power_target_pct else "1.00")

        elif st == "interval_off":
            # Standalone recovery interval (already consumed by on step normally)
            elem = ET.SubElement(wo, "SteadyState")
            elem.set("Duration", str(step.duration_seconds))
            elem.set("Power", f"{step.power_target_pct:.2f}" if step.power_target_pct else "0.50")

        elif st == "free_ride":
            elem = ET.SubElement(wo, "FreeRide")
            elem.set("Duration", str(step.duration_seconds))

        elif st == "ramp":
            elem = ET.SubElement(wo, "Warmup")
            elem.set("Duration", str(step.duration_seconds))
            elem.set("PowerLow", f"{step.power_low_pct:.2f}" if step.power_low_pct else "0.40")
            elem.set("PowerHigh", f"{step.power_high_pct:.2f}" if step.power_high_pct else "1.00")

        i += 1

    # Pretty print
    xml_str = ET.tostring(root, encoding="unicode")
    dom = minidom.parseString(xml_str)
    return dom.toprettyxml(indent="  ", encoding=None)


# === ERG Export (Absolute Watts) ===

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

    current_time = 0.0  # minutes
    # In the order they are ridden: a 2x20 is on, off, on, off. Repeating
    # each step on its own exported 40 minutes straight at threshold, then
    # one recovery (found 16 Oct 2026).
    steps = ride_order(sorted(workout.steps, key=lambda s: s.step_order))

    for step in steps:
        duration_min = step.duration_seconds / 60.0

        if step.step_type in ("warmup", "cooldown", "ramp"):
            low_pct = step.power_low_pct or 0.40
            high_pct = step.power_high_pct or step.power_target_pct or 0.70
            low_w = round(low_pct * ftp)
            high_w = round(high_pct * ftp)
            lines.append(f"{current_time:.2f}\t{low_w}")
            current_time += duration_min
            lines.append(f"{current_time:.2f}\t{high_w}")
        elif step.step_type == "free_ride":
            watts = round(0.50 * ftp)
            lines.append(f"{current_time:.2f}\t{watts}")
            current_time += duration_min
            lines.append(f"{current_time:.2f}\t{watts}")
        else:
            pct = step.power_target_pct or 0.65
            watts = round(pct * ftp)
            lines.append(f"{current_time:.2f}\t{watts}")
            current_time += duration_min
            lines.append(f"{current_time:.2f}\t{watts}")

    lines.append("[END COURSE DATA]")

    # Course text for step labels
    lines.append("[COURSE TEXT]")
    elapsed = 0
    for step in steps:
        label = step.notes or step.step_type.replace("_", " ").title()
        lines.append(f"{elapsed}\t{label}\t10")
        elapsed += step.duration_seconds
    lines.append("[END COURSE TEXT]")

    return "\n".join(lines)


# === MRC Export (Percent FTP) ===

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

    current_time = 0.0
    steps = ride_order(sorted(workout.steps, key=lambda s: s.step_order))

    for step in steps:
        duration_min = step.duration_seconds / 60.0

        if step.step_type in ("warmup", "cooldown", "ramp"):
            low_pct = (step.power_low_pct or 0.40) * 100
            high_pct = (step.power_high_pct or step.power_target_pct or 0.70) * 100
            lines.append(f"{current_time:.2f}\t{low_pct:.0f}")
            current_time += duration_min
            lines.append(f"{current_time:.2f}\t{high_pct:.0f}")
        elif step.step_type == "free_ride":
            lines.append(f"{current_time:.2f}\t50")
            current_time += duration_min
            lines.append(f"{current_time:.2f}\t50")
        else:
            pct = (step.power_target_pct or 0.65) * 100
            lines.append(f"{current_time:.2f}\t{pct:.0f}")
            current_time += duration_min
            lines.append(f"{current_time:.2f}\t{pct:.0f}")

    lines.append("[END COURSE DATA]")

    lines.append("[COURSE TEXT]")
    elapsed = 0
    for step in steps:
        label = step.notes or step.step_type.replace("_", " ").title()
        lines.append(f"{elapsed}\t{label}\t10")
        elapsed += step.duration_seconds
    lines.append("[END COURSE TEXT]")

    return "\n".join(lines)


# === FIT Workout Export (Binary) ===

def workout_to_fit(workout: Workout, ftp: int = 200) -> bytes:
    """
    Convert a workout to Garmin FIT workout format.

    FIT is a binary format used by Garmin, Wahoo, and Hammerhead devices.
    This builds a minimal valid FIT file with workout and workout_step messages.
    """
    import struct

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

    # Flatten steps (expand repeats for intervals)
    flat_steps = []
    steps = sorted(workout.steps, key=lambda s: s.step_order)
    i = 0
    while i < len(steps):
        step = steps[i]
        if step.step_type == "interval_on":
            off_step = steps[i + 1] if i + 1 < len(steps) and steps[i + 1].step_type == "interval_off" else None
            repeats = step.repeat_count or 1
            for _ in range(repeats):
                flat_steps.append(step)
                if off_step:
                    flat_steps.append(off_step)
            if off_step:
                i += 1
        else:
            flat_steps.append(step)
        i += 1

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
    workout_name = (workout.title or "Workout")[:16].encode("utf-8")
    workout_name_padded = workout_name + b"\x00" * (16 - len(workout_name))
    wo_fields = [
        (4, 1, 0),    # sport: enum - 2=cycling
        (8, 2, 132),  # num_valid_steps: uint16
        (0, 16, 7),   # wkt_name: string (16 bytes)
    ]
    records += _fit_definition(1, MESG_WORKOUT, wo_fields)
    records += _fit_data_record(1, struct.pack("<BH", 2, len(flat_steps)) + workout_name_padded)

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
        name = (step.notes or step.step_type.replace("_", " "))[:16].encode("utf-8")
        name_padded = name + b"\x00" * (16 - len(name))

        duration_ms = step.duration_seconds * 1000
        intensity = _step_intensity(step.step_type)

        # Power targets: FIT uses watts + 1000 offset for custom targets
        if step.step_type in ("warmup", "cooldown", "ramp"):
            low_pct = step.power_low_pct or 0.40
            high_pct = step.power_high_pct or step.power_target_pct or 0.70
            low_w = round(low_pct * ftp) + 1000
            high_w = round(high_pct * ftp) + 1000
        elif step.step_type == "free_ride":
            low_w = 0
            high_w = 0
        else:
            pct = step.power_target_pct or 0.65
            target_w = round(pct * ftp)
            # ±5W range
            low_w = target_w - 5 + 1000
            high_w = target_w + 5 + 1000

        data = name_padded + struct.pack(
            "<BIBIIIBH",
            DURATION_TIME,     # duration_type
            duration_ms,       # duration_value
            TARGET_POWER,      # target_type
            0,                 # target_value (0 = custom)
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
    import struct
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
    import struct

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
