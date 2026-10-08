/**
 * Web Bluetooth utilities for cycling sensors and smart trainers.
 *
 * Supported devices:
 * - Heart Rate Monitor (BLE HRM service 0x180D)
 * - Cycling Power Meter (BLE CPS service 0x1818)
 * - Cadence Sensor (BLE CSC service 0x1816)
 * - Smart Trainer / FTMS (BLE FTMS service 0x1826) with ERG mode
 */

// === Service & Characteristic UUIDs ===

export const BLE_SERVICES = {
  heartRate: 0x180d,
  cyclingPower: 0x1818,
  cyclingSpeedCadence: 0x1816,
  fitnessMachine: 0x1826,
} as const;

export const BLE_CHARACTERISTICS = {
  heartRateMeasurement: 0x2a37,
  cyclingPowerMeasurement: 0x2a63,
  cscMeasurement: 0x2a5b,
  indoorBikeData: 0x2ad2,
  fitnessMachineControlPoint: 0x2ad9,
  fitnessMachineStatus: 0x2ada,
  supportedPowerRange: 0x2ad4,
} as const;

// === Type Definitions ===

export interface HeartRateData {
  heartRate: number;
  contactDetected?: boolean;
}

export interface PowerData {
  instantaneousPower: number;
  crankRevolutions?: number;
  lastCrankEventTime?: number;
}

export interface CadenceData {
  cadence: number;
  crankRevolutions: number;
  lastCrankEventTime: number;
}

export interface IndoorBikeData {
  speed?: number;
  cadence?: number;
  power?: number;
  heartRate?: number;
  totalDistance?: number;
}

export type DeviceType = "heartRate" | "power" | "cadence" | "trainer";

// === Parser Functions ===

export function parseHeartRate(value: DataView): HeartRateData {
  const flags = value.getUint8(0);
  const is16Bit = flags & 0x01;
  const contactDetected = (flags & 0x06) === 0x06;
  const heartRate = is16Bit ? value.getUint16(1, true) : value.getUint8(1);

  return { heartRate, contactDetected };
}

export function parseCyclingPower(value: DataView): PowerData {
  const flags = value.getUint16(0, true);
  const instantaneousPower = value.getInt16(2, true);

  const result: PowerData = { instantaneousPower };

  // Check if crank revolution data is present (bit 5)
  if (flags & 0x20) {
    let offset = 4;
    // Skip pedal power balance if present (bit 0)
    if (flags & 0x01) offset += 1;
    // Skip accumulated torque if present (bit 2)
    if (flags & 0x04) offset += 2;
    // Skip wheel revolution data if present (bit 4)
    if (flags & 0x10) offset += 6;

    result.crankRevolutions = value.getUint16(offset, true);
    result.lastCrankEventTime = value.getUint16(offset + 2, true);
  }

  return result;
}

export function parseCSCMeasurement(value: DataView): {
  wheelRevolutions?: number;
  lastWheelEventTime?: number;
  crankRevolutions?: number;
  lastCrankEventTime?: number;
} {
  const flags = value.getUint8(0);
  let offset = 1;
  const result: {
    wheelRevolutions?: number;
    lastWheelEventTime?: number;
    crankRevolutions?: number;
    lastCrankEventTime?: number;
  } = {};

  // Wheel revolution data (bit 0)
  if (flags & 0x01) {
    result.wheelRevolutions = value.getUint32(offset, true);
    result.lastWheelEventTime = value.getUint16(offset + 4, true);
    offset += 6;
  }

  // Crank revolution data (bit 1)
  if (flags & 0x02) {
    result.crankRevolutions = value.getUint16(offset, true);
    result.lastCrankEventTime = value.getUint16(offset + 2, true);
  }

  return result;
}

export function parseIndoorBikeData(value: DataView): IndoorBikeData {
  const flags = value.getUint16(0, true);
  const result: IndoorBikeData = {};
  let offset = 2;

  // Instantaneous Speed (bit 0 = 0 means it IS present, inverted flag)
  if (!(flags & 0x01)) {
    result.speed = value.getUint16(offset, true) * 0.01; // km/h
    offset += 2;
  }

  // Average Speed (bit 1)
  if (flags & 0x02) {
    offset += 2; // skip
  }

  // Instantaneous Cadence (bit 2)
  if (flags & 0x04) {
    result.cadence = value.getUint16(offset, true) * 0.5; // rpm
    offset += 2;
  }

  // Average Cadence (bit 3)
  if (flags & 0x08) {
    offset += 2; // skip
  }

  // Total Distance (bit 4)
  if (flags & 0x10) {
    result.totalDistance =
      value.getUint16(offset, true) + (value.getUint8(offset + 2) << 16); // meters
    offset += 3;
  }

  // Resistance Level (bit 5)
  if (flags & 0x20) {
    offset += 2;
  }

  // Instantaneous Power (bit 6)
  if (flags & 0x40) {
    result.power = value.getInt16(offset, true);
    offset += 2;
  }

  // Average Power (bit 7)
  if (flags & 0x80) {
    offset += 2;
  }

  // Expended Energy (bit 8)
  if (flags & 0x100) {
    offset += 5; // total, per hour, per minute
  }

  // Heart Rate (bit 9)
  if (flags & 0x200) {
    result.heartRate = value.getUint8(offset);
  }

  return result;
}

// === Cadence Calculator ===

export class CadenceCalculator {
  private lastCrankRevs: number | null = null;
  private lastCrankTime: number | null = null;

  /**
   * Calculate RPM from cumulative crank revolutions and event time.
   * Time is in 1/1024 second units (CSC) or 1/2048 second units (power meter).
   */
  calculate(
    crankRevolutions: number,
    lastCrankEventTime: number,
    timeResolution: number = 1024
  ): number | null {
    if (this.lastCrankRevs === null || this.lastCrankTime === null) {
      this.lastCrankRevs = crankRevolutions;
      this.lastCrankTime = lastCrankEventTime;
      return null;
    }

    let deltaRevs = crankRevolutions - this.lastCrankRevs;
    let deltaTime = lastCrankEventTime - this.lastCrankTime;

    // Handle rollover
    if (deltaRevs < 0) deltaRevs += 0x10000;
    if (deltaTime < 0) deltaTime += 0x10000;

    this.lastCrankRevs = crankRevolutions;
    this.lastCrankTime = lastCrankEventTime;

    if (deltaTime === 0 || deltaRevs === 0) return 0;

    const rpm = (deltaRevs / (deltaTime / timeResolution)) * 60;
    return Math.round(rpm);
  }

  reset() {
    this.lastCrankRevs = null;
    this.lastCrankTime = null;
  }
}

// === ERG Safety Limits ===
// The last line of defence: whatever the session, the page or a bug asks
// for, no Set Target Power command leaves this file above the ceiling.

/** ERG never holds a trainer above this multiple of FTP. Steps above it
    (sprints) run with ERG released, against the rider's own resistance. */
export const ERG_CAP_PCT = 1.3;

/** Absolute ceiling for any ERG target, FTP or no FTP. */
export const ERG_ABSOLUTE_MAX_WATTS = 1000;

let ergCeilingWatts: number | null = null;

/** The ERG ceiling in watts for this FTP. A cap above ERG_CAP_PCT is ignored. */
export function ergCeilingFor(ftp: number, capPct: number = ERG_CAP_PCT): number {
  const pct = Number.isFinite(capPct) ? Math.min(capPct, ERG_CAP_PCT) : ERG_CAP_PCT;
  return Math.floor(Math.max(0, ftp || 0) * pct);
}

/** Ride mode sets this from the rider's FTP; null falls back to the absolute
    ceiling. */
export function setErgCeiling(watts: number | null): void {
  ergCeilingWatts =
    watts === null || !Number.isFinite(watts) ? null : Math.max(0, Math.floor(watts));
}

export function getErgCeiling(): number | null {
  return ergCeilingWatts;
}

/** A target the trainer may be given: whole watts, 0 or more, never above
    the ceiling (or the absolute ceiling). */
export function clampErgWatts(
  watts: number,
  ceiling: number | null = ergCeilingWatts
): number {
  if (!Number.isFinite(watts) || watts <= 0) return 0;
  const limit = Math.min(ceiling ?? ERG_ABSOLUTE_MAX_WATTS, ERG_ABSOLUTE_MAX_WATTS);
  return Math.max(0, Math.min(Math.round(watts), Math.floor(limit)));
}

/**
 * Runs trainer commands one at a time, in the order given. Web Bluetooth
 * rejects a write while another is in flight on the same characteristic, so
 * a pause sent mid-write would otherwise be dropped and the trainer left
 * holding the old target. A failed command never blocks the ones after it.
 */
export function createCommandQueue(): (cmd: () => Promise<void>) => Promise<void> {
  let tail: Promise<void> = Promise.resolve();
  return (cmd) => {
    tail = tail.then(cmd).catch((e) => {
      console.error("Trainer command failed:", e);
    });
    return tail;
  };
}

// === FTMS Control Point Commands ===

export function ftmsRequestControl(): Uint8Array {
  return new Uint8Array([0x00]);
}

export function ftmsReset(): Uint8Array {
  return new Uint8Array([0x01]);
}

export function ftmsStartOrResume(): Uint8Array {
  return new Uint8Array([0x07]);
}

export function ftmsStop(): Uint8Array {
  return new Uint8Array([0x08, 0x01]); // 0x01 = stop
}

export function ftmsPause(): Uint8Array {
  return new Uint8Array([0x08, 0x02]); // 0x02 = pause
}

export function ftmsSetTargetPower(watts: number): Uint8Array {
  const buf = new ArrayBuffer(3);
  const view = new DataView(buf);
  view.setUint8(0, 0x05); // Set Target Power opcode
  view.setInt16(1, clampErgWatts(watts), true); // never above the ERG ceiling
  return new Uint8Array(buf);
}

export function ftmsSetSimulation(
  windSpeed: number = 0,
  grade: number = 0,
  crr: number = 0.004,
  cw: number = 0.51
): Uint8Array {
  const buf = new ArrayBuffer(7);
  const view = new DataView(buf);
  view.setUint8(0, 0x11); // Set Indoor Bike Simulation opcode
  view.setInt16(1, Math.round(windSpeed * 1000), true); // 0.001 m/s
  view.setInt16(3, Math.round(grade * 100), true); // 0.01%
  view.setUint8(5, Math.round(crr * 10000)); // 0.0001
  view.setUint8(6, Math.round(cw * 100)); // 0.01 kg/m
  return new Uint8Array(buf);
}

// === Bluetooth Availability Check ===

export function isBluetoothSupported(): boolean {
  return typeof navigator !== "undefined" && "bluetooth" in navigator;
}
