"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  BLE_SERVICES,
  BLE_CHARACTERISTICS,
  CadenceCalculator,
  parseHeartRate,
  parseCyclingPower,
  parseCSCMeasurement,
  parseIndoorBikeData,
  ftmsRequestControl,
  ftmsStartOrResume,
  ftmsSetTargetPower,
  ftmsStop,
  isBluetoothSupported,
  type DeviceType,
} from "@/lib/bluetooth";
import { TrainerControl } from "@/lib/rideSafety";

interface DeviceConnection {
  device: BluetoothDevice;
  server: BluetoothRemoteGATTServer;
}

interface BluetoothDeviceState {
  connected: boolean;
  name: string | null;
  battery?: number;
}

export interface BluetoothState {
  isSupported: boolean;
  heartRate: BluetoothDeviceState & { value: number | null };
  power: BluetoothDeviceState & { value: number | null };
  cadence: BluetoothDeviceState & { value: number | null };
  trainer: BluetoothDeviceState & {
    ergMode: boolean;
    targetPower: number | null;
  };
}

export interface BluetoothActions {
  connectHeartRate: () => Promise<void>;
  connectPower: () => Promise<void>;
  connectCadence: () => Promise<void>;
  connectTrainer: () => Promise<void>;
  disconnectDevice: (type: DeviceType) => void;
  setTargetPower: (watts: number) => Promise<void>;
  stopTrainer: () => Promise<void>;
  /** Sensors go at once; the trainer is let go first (see TrainerControl). */
  disconnectAll: () => void;
}

export interface BluetoothOptions {
  /** What the trainer drops to before it is let go: 40% of FTP. Null sends
      the FTMS stop alone. */
  releaseWatts?: number | null;
}

export function useBluetooth(
  options: BluetoothOptions = {}
): [BluetoothState, BluetoothActions] {
  const [hrState, setHrState] = useState<BluetoothState["heartRate"]>({
    connected: false,
    name: null,
    value: null,
  });
  const [powerState, setPowerState] = useState<BluetoothState["power"]>({
    connected: false,
    name: null,
    value: null,
  });
  const [cadenceState, setCadenceState] = useState<BluetoothState["cadence"]>({
    connected: false,
    name: null,
    value: null,
  });
  const [trainerState, setTrainerState] = useState<BluetoothState["trainer"]>({
    connected: false,
    name: null,
    ergMode: false,
    targetPower: null,
  });

  const hrConnection = useRef<DeviceConnection | null>(null);
  const powerConnection = useRef<DeviceConnection | null>(null);
  const cadenceConnection = useRef<DeviceConnection | null>(null);
  const trainerConnection = useRef<DeviceConnection | null>(null);
  // Every control point write, in order, through one queue. It also knows
  // whether the trainer is stopped (a sprint release, Stop, the end), so the
  // next target is preceded by Start/Resume.
  const trainerControlRef = useRef<TrainerControl | null>(null);
  if (trainerControlRef.current === null) {
    trainerControlRef.current = new TrainerControl({
      setTargetPower: ftmsSetTargetPower,
      stop: ftmsStop,
      startOrResume: ftmsStartOrResume,
    });
  }
  const trainer = trainerControlRef.current;
  const releaseWatts = useRef<number | null>(options.releaseWatts ?? null);
  releaseWatts.current = options.releaseWatts ?? null;
  const cadenceCalc = useRef(new CadenceCalculator());
  const powerCadenceCalc = useRef(new CadenceCalculator());

  // Leaving the page (a link, the back button) mid-ride: sensors disconnect
  // at once, but the trainer is let go first, 40% then FTMS stop, and only
  // then disconnected. A bare disconnect can leave a trainer holding the
  // last ERG target with the rider still on it.
  useEffect(() => {
    return () => {
      [hrConnection, powerConnection, cadenceConnection].forEach((ref) => {
        if (ref.current?.server.connected) {
          ref.current.server.disconnect();
        }
      });
      const conn = trainerConnection.current;
      if (conn) {
        void trainer.releaseThenDisconnect(releaseWatts.current, () => {
          if (conn.server.connected) conn.server.disconnect();
        });
      }
    };
  }, [trainer]);

  const handleDisconnect = useCallback(
    (type: DeviceType) => () => {
      switch (type) {
        case "heartRate":
          setHrState({ connected: false, name: null, value: null });
          hrConnection.current = null;
          break;
        case "power":
          setPowerState({ connected: false, name: null, value: null });
          powerConnection.current = null;
          powerCadenceCalc.current.reset();
          break;
        case "cadence":
          setCadenceState({ connected: false, name: null, value: null });
          cadenceConnection.current = null;
          cadenceCalc.current.reset();
          break;
        case "trainer":
          setTrainerState({
            connected: false,
            name: null,
            ergMode: false,
            targetPower: null,
          });
          trainerConnection.current = null;
          trainer.detach();
          // Clear trainer-derived sensor readings
          if (!powerConnection.current) {
            setPowerState({ connected: false, name: null, value: null });
          }
          if (!cadenceConnection.current) {
            setCadenceState({ connected: false, name: null, value: null });
          }
          if (!hrConnection.current) {
            setHrState({ connected: false, name: null, value: null });
          }
          break;
      }
    },
    [trainer]
  );

  const connectHeartRate = useCallback(async () => {
    if (!isBluetoothSupported()) return;

    const device = await navigator.bluetooth.requestDevice({
      filters: [{ services: [BLE_SERVICES.heartRate] }],
    });

    device.addEventListener(
      "gattserverdisconnected",
      handleDisconnect("heartRate")
    );

    const server = await device.gatt!.connect();
    hrConnection.current = { device, server };

    const service = await server.getPrimaryService(BLE_SERVICES.heartRate);
    const char = await service.getCharacteristic(
      BLE_CHARACTERISTICS.heartRateMeasurement
    );

    await char.startNotifications();
    char.addEventListener("characteristicvaluechanged", (e) => {
      const target = e.target as BluetoothRemoteGATTCharacteristic;
      if (!target.value) return;
      const data = parseHeartRate(target.value);
      setHrState((prev) => ({
        ...prev,
        connected: true,
        name: device.name || "HR Monitor",
        value: data.heartRate,
      }));
    });

    setHrState((prev) => ({
      ...prev,
      connected: true,
      name: device.name || "HR Monitor",
    }));
  }, [handleDisconnect]);

  const connectPower = useCallback(async () => {
    if (!isBluetoothSupported()) return;

    const device = await navigator.bluetooth.requestDevice({
      filters: [{ services: [BLE_SERVICES.cyclingPower] }],
    });

    device.addEventListener(
      "gattserverdisconnected",
      handleDisconnect("power")
    );

    const server = await device.gatt!.connect();
    powerConnection.current = { device, server };

    const service = await server.getPrimaryService(BLE_SERVICES.cyclingPower);
    const char = await service.getCharacteristic(
      BLE_CHARACTERISTICS.cyclingPowerMeasurement
    );

    powerCadenceCalc.current.reset();

    await char.startNotifications();
    char.addEventListener("characteristicvaluechanged", (e) => {
      const target = e.target as BluetoothRemoteGATTCharacteristic;
      if (!target.value) return;
      const data = parseCyclingPower(target.value);
      setPowerState((prev) => ({
        ...prev,
        connected: true,
        name: device.name || "Power Meter",
        value: data.instantaneousPower,
      }));

      // Also extract cadence from power meter if available and no separate cadence sensor
      if (
        data.crankRevolutions !== undefined &&
        data.lastCrankEventTime !== undefined
      ) {
        const rpm = powerCadenceCalc.current.calculate(
          data.crankRevolutions,
          data.lastCrankEventTime,
          2048 // Power meter uses 1/2048s resolution
        );
        if (rpm !== null && !cadenceConnection.current) {
          setCadenceState((prev) => ({
            ...prev,
            value: rpm,
            // Don't set connected - it's derived from power meter
          }));
        }
      }
    });

    setPowerState((prev) => ({
      ...prev,
      connected: true,
      name: device.name || "Power Meter",
    }));
  }, [handleDisconnect]);

  const connectCadence = useCallback(async () => {
    if (!isBluetoothSupported()) return;

    const device = await navigator.bluetooth.requestDevice({
      filters: [{ services: [BLE_SERVICES.cyclingSpeedCadence] }],
    });

    device.addEventListener(
      "gattserverdisconnected",
      handleDisconnect("cadence")
    );

    const server = await device.gatt!.connect();
    cadenceConnection.current = { device, server };

    const service = await server.getPrimaryService(
      BLE_SERVICES.cyclingSpeedCadence
    );
    const char = await service.getCharacteristic(
      BLE_CHARACTERISTICS.cscMeasurement
    );

    cadenceCalc.current.reset();

    await char.startNotifications();
    char.addEventListener("characteristicvaluechanged", (e) => {
      const target = e.target as BluetoothRemoteGATTCharacteristic;
      if (!target.value) return;
      const data = parseCSCMeasurement(target.value);

      if (
        data.crankRevolutions !== undefined &&
        data.lastCrankEventTime !== undefined
      ) {
        const rpm = cadenceCalc.current.calculate(
          data.crankRevolutions,
          data.lastCrankEventTime,
          1024
        );
        if (rpm !== null) {
          setCadenceState((prev) => ({
            ...prev,
            connected: true,
            name: device.name || "Cadence Sensor",
            value: rpm,
          }));
        }
      }
    });

    setCadenceState((prev) => ({
      ...prev,
      connected: true,
      name: device.name || "Cadence Sensor",
    }));
  }, [handleDisconnect]);

  const connectTrainer = useCallback(async () => {
    if (!isBluetoothSupported()) return;

    const device = await navigator.bluetooth.requestDevice({
      filters: [{ services: [BLE_SERVICES.fitnessMachine] }],
      optionalServices: [BLE_SERVICES.heartRate],
    });

    device.addEventListener(
      "gattserverdisconnected",
      handleDisconnect("trainer")
    );

    const server = await device.gatt!.connect();
    trainerConnection.current = { device, server };

    const service = await server.getPrimaryService(BLE_SERVICES.fitnessMachine);

    // Subscribe to Indoor Bike Data
    try {
      const bikeData = await service.getCharacteristic(
        BLE_CHARACTERISTICS.indoorBikeData
      );
      await bikeData.startNotifications();
      bikeData.addEventListener("characteristicvaluechanged", (e) => {
        const target = e.target as BluetoothRemoteGATTCharacteristic;
        if (!target.value) return;
        const data = parseIndoorBikeData(target.value);

        // Update power/cadence/HR from trainer if no dedicated sensor
        const trainerName = device.name || "Smart Trainer";
        if (data.power !== undefined && !powerConnection.current) {
          setPowerState((prev) => ({
            ...prev,
            connected: true,
            name: prev.name || `${trainerName} (power)`,
            value: data.power!,
          }));
        }
        if (data.cadence !== undefined && !cadenceConnection.current) {
          setCadenceState((prev) => ({
            ...prev,
            connected: true,
            name: prev.name || `${trainerName} (cadence)`,
            value: data.cadence!,
          }));
        }
        if (data.heartRate !== undefined && !hrConnection.current) {
          setHrState((prev) => ({
            ...prev,
            connected: true,
            name: prev.name || `${trainerName} (HR)`,
            value: data.heartRate!,
          }));
        }
      });
    } catch {
      // Some trainers don't support Indoor Bike Data
    }

    // Get Control Point for ERG mode. ERG only counts once the trainer has
    // handed over control and started.
    try {
      const controlPoint = await service.getCharacteristic(
        BLE_CHARACTERISTICS.fitnessMachineControlPoint
      );
      await controlPoint.startNotifications();
      const write = (bytes: Uint8Array) =>
        controlPoint.writeValue(bytes as unknown as BufferSource);

      // Request control
      await write(ftmsRequestControl());
      // Small delay for trainer to process
      await new Promise((r) => setTimeout(r, 200));
      // Start
      await write(ftmsStartOrResume());
      trainer.attach(write);
    } catch {
      // Control point not available: the trainer may be read-only
    }

    setTrainerState((prev) => ({
      ...prev,
      connected: true,
      name: device.name || "Smart Trainer",
      ergMode: trainer.attached,
    }));
  }, [handleDisconnect, trainer]);

  const disconnectDevice = useCallback(
    (type: DeviceType) => {
      if (type === "trainer") {
        // Let go first, then disconnect (see the unmount cleanup above).
        const conn = trainerConnection.current;
        if (!conn) {
          handleDisconnect("trainer")();
          return;
        }
        void trainer.releaseThenDisconnect(releaseWatts.current, () => {
          if (conn.server.connected) conn.server.disconnect();
          handleDisconnect("trainer")();
        });
        return;
      }
      const refs: Record<DeviceType, React.RefObject<DeviceConnection | null>> =
        {
          heartRate: hrConnection,
          power: powerConnection,
          cadence: cadenceConnection,
          trainer: trainerConnection,
        };
      const ref = refs[type];
      if (ref.current?.server.connected) {
        ref.current.server.disconnect();
      }
      handleDisconnect(type)();
    },
    [handleDisconnect, trainer]
  );

  const setTargetPower = useCallback(
    async (watts: number) => {
      if (await trainer.setTarget(watts)) {
        setTrainerState((prev) => ({ ...prev, targetPower: watts }));
      }
    },
    [trainer]
  );

  const stopTrainer = useCallback(() => trainer.stop(), [trainer]);

  const disconnectAll = useCallback(() => {
    (["heartRate", "power", "cadence", "trainer"] as DeviceType[]).forEach(
      disconnectDevice
    );
  }, [disconnectDevice]);

  const state: BluetoothState = {
    isSupported: isBluetoothSupported(),
    heartRate: hrState,
    power: powerState,
    cadence: cadenceState,
    trainer: trainerState,
  };

  const actions: BluetoothActions = {
    connectHeartRate,
    connectPower,
    connectCadence,
    connectTrainer,
    disconnectDevice,
    setTargetPower,
    stopTrainer,
    disconnectAll,
  };

  return [state, actions];
}
