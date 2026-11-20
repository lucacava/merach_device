"""
Minimal GATT server on CoreBluetooth (macOS) for standard fitness sensors.

Why not ``bless``: bless declares services with full 128-bit UUIDs (16 bytes each). With a 13-character
local name the service list no longer fits in the advertising packet and macOS silently drops it, while
Garmin and Zwift discover sensors precisely by their 16-bit service UUID (e.g. 0x1818). This module uses
16-bit UUIDs everywhere.

Known macOS limitation: when acting as a peripheral, CoreBluetooth only advertises the local name and the
service UUID list. Service data (required by FTMS receivers such as Garmin to list a trainer) is accepted
without error but never transmitted — hence the ESP32 bridge (``merach.esp32_bridge``).

CoreBluetooth callbacks arrive on a dispatch queue and are forwarded to the asyncio event loop.
"""
from __future__ import annotations

import asyncio

import objc  # type: ignore
from CoreBluetooth import (  # type: ignore
    CBAdvertisementDataLocalNameKey,
    CBAdvertisementDataServiceDataKey,
    CBAdvertisementDataServiceUUIDsKey,
    CBATTErrorSuccess,
    CBAttributePermissionsReadable,
    CBAttributePermissionsWriteable,
    CBCharacteristicPropertyIndicate,
    CBCharacteristicPropertyNotify,
    CBCharacteristicPropertyRead,
    CBCharacteristicPropertyWrite,
    CBManagerStatePoweredOn,
    CBMutableCharacteristic,
    CBMutableService,
    CBPeripheralManager,
    CBUUID,
)
from Foundation import NSData, NSObject  # type: ignore
from libdispatch import DISPATCH_QUEUE_SERIAL, dispatch_queue_create  # type: ignore

_PROTOCOL = objc.protocolNamed("CBPeripheralManagerDelegate")


class ClaudeMerachPeripheralDelegate(NSObject, protocols=[_PROTOCOL]):  # type: ignore
    """Objective-C delegate: forwards every event to ``self.cb(event, *args)``."""

    def peripheralManagerDidUpdateState_(self, pm):  # noqa: N802
        self.cb("state", pm.state())

    def peripheralManager_didAddService_error_(self, pm, service, error):  # noqa: N802
        self.cb("added", str(service.UUID().UUIDString()).upper(), error)

    def peripheralManagerDidStartAdvertising_error_(self, pm, error):  # noqa: N802
        self.cb("advertising", error)

    def peripheralManager_central_didSubscribeToCharacteristic_(self, pm, central, ch):  # noqa: N802
        self.cb("sub", str(central.identifier().UUIDString()), str(ch.UUID().UUIDString()).upper())

    def peripheralManager_central_didUnsubscribeFromCharacteristic_(self, pm, central, ch):  # noqa: N802
        self.cb("unsub", str(central.identifier().UUIDString()), str(ch.UUID().UUIDString()).upper())

    def peripheralManagerIsReadyToUpdateSubscribers_(self, pm):  # noqa: N802
        pass

    def peripheralManager_didReceiveWriteRequests_(self, pm, requests):  # noqa: N802
        # acknowledge right away (one response covers the whole batch) and hand the values to asyncio
        for req in requests:
            self.cb("write", str(req.characteristic().UUID().UUIDString()).upper(), bytes(req.value() or b""))
        if requests:
            pm.respondToRequest_withResult_(requests[0], CBATTErrorSuccess)


def _nsdata(b: bytes) -> NSData:
    return NSData.dataWithBytes_length_(b, len(b))


class GattPeripheral:
    """One CBPeripheralManager per process: ``start()`` publishes services, ``stop()`` removes them."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self._powered = asyncio.Event()
        self._added: dict[str, asyncio.Event] = {}
        self._add_errors: dict[str, object] = {}
        self._adv = asyncio.Event()
        self._adv_error = None
        self.subscriptions: dict[str, set[str]] = {}
        self.chars: dict[str, object] = {}
        self.on_write = None  # callable(characteristic_uuid, value) for "write" characteristics
        self.service_data_ok: bool | None = None
        self.service_data_error = ""
        self.delegate = ClaudeMerachPeripheralDelegate.alloc().init()
        self.delegate.cb = self._from_ble
        self.pm = CBPeripheralManager.alloc().initWithDelegate_queue_(
            self.delegate, dispatch_queue_create(b"Better_Merach", DISPATCH_QUEUE_SERIAL))

    # ---- events from the Bluetooth queue → asyncio loop
    def _from_ble(self, event: str, *args) -> None:
        self.loop.call_soon_threadsafe(self._handle, event, args)

    def _handle(self, event: str, args: tuple) -> None:
        if event == "state":
            (self._powered.set if args[0] == CBManagerStatePoweredOn else self._powered.clear)()
        elif event == "added":
            uuid, err = args
            if err:
                self._add_errors[uuid] = err
            ev = self._added.get(uuid)
            if ev:
                ev.set()
        elif event == "advertising":
            self._adv_error = args[0]
            self._adv.set()
        elif event == "sub":
            self.subscriptions.setdefault(args[0], set()).add(args[1])
        elif event == "write":
            if self.on_write:
                self.on_write(args[0], args[1])
        elif event == "unsub":
            subs = self.subscriptions.get(args[0])
            if subs is not None:
                subs.discard(args[1])
                if not subs:
                    del self.subscriptions[args[0]]

    # ---- API
    async def start(self, name: str, services: list[tuple[str, list[tuple[str, str, bytes | None]]]],
                    advertise: list[str] | None = None, service_data: dict[str, bytes] | None = None,
                    timeout: float = 10) -> None:
        """Publish services and start advertising.

        ``services`` is ``[(service_uuid, [(characteristic_uuid, kind, static_value), ...]), ...]`` where
        ``kind`` is ``"notify"`` (dynamic), ``"read"`` (static value, answered by CoreBluetooth itself) or
        ``"write"`` (write + indicate, e.g. a control point).

        ``advertise`` lists the services announced in the advertisement (default: all). The others stay in
        the GATT database, but receivers that classify sensors from the advertisement (e.g. Garmin) ignore
        them.

        ``service_data`` is attempted as advertisement service data; macOS accepts it but does not transmit
        it (see module docstring). ``service_data_ok`` reports whether the call itself succeeded.
        """
        await asyncio.wait_for(self._powered.wait(), timeout)
        for svc_uuid, chars in services:
            svc_uuid = svc_uuid.upper()
            service = CBMutableService.alloc().initWithType_primary_(CBUUID.UUIDWithString_(svc_uuid), True)
            cb_chars = []
            for ch_uuid, kind, value in chars:
                perms = CBAttributePermissionsReadable
                if kind == "notify":
                    props, val = CBCharacteristicPropertyNotify, None
                elif kind == "write":
                    props, val = CBCharacteristicPropertyWrite | CBCharacteristicPropertyIndicate, None
                    perms = CBAttributePermissionsWriteable
                else:  # static value: CoreBluetooth answers reads on its own
                    props, val = CBCharacteristicPropertyRead, _nsdata(value or b"")
                ch = CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
                    CBUUID.UUIDWithString_(ch_uuid.upper()), props, val, perms)
                self.chars[ch_uuid.upper()] = ch
                cb_chars.append(ch)
            service.setCharacteristics_(cb_chars)
            self._added[svc_uuid] = asyncio.Event()
            self.pm.addService_(service)
            await asyncio.wait_for(self._added[svc_uuid].wait(), timeout)
            if svc_uuid in self._add_errors:
                raise RuntimeError(f"service {svc_uuid} not added: {self._add_errors.pop(svc_uuid)}")
        base = {
            CBAdvertisementDataLocalNameKey: name,
            CBAdvertisementDataServiceUUIDsKey: [CBUUID.UUIDWithString_(s.upper())
                                                 for s in (advertise or [svc for svc, _ in services])],
        }
        self.service_data_ok = None
        if service_data:
            adv = dict(base)
            # when advertising, macOS expects the keys as strings ("1826"), not CBUUID objects
            adv[CBAdvertisementDataServiceDataKey] = {k.upper(): _nsdata(v) for k, v in service_data.items()}
            try:
                await self._advertise(adv, timeout)
                self.service_data_ok = True
                return
            except Exception as e:  # noqa: BLE001
                self.service_data_ok = False
                self.service_data_error = str(e)
                if self.pm.isAdvertising():
                    self.pm.stopAdvertising()
        await self._advertise(base, timeout)

    async def _advertise(self, data: dict, timeout: float) -> None:
        self._adv.clear()
        self._adv_error = None
        self.pm.startAdvertising_(data)
        await asyncio.wait_for(self._adv.wait(), timeout)
        if self._adv_error:
            raise RuntimeError(f"advertising did not start: {self._adv_error}")

    def notify(self, ch_uuid: str, data: bytes) -> bool:
        ch = self.chars.get(ch_uuid.upper())
        if ch is None:
            return False
        return bool(self.pm.updateValue_forCharacteristic_onSubscribedCentrals_(_nsdata(data), ch, None))

    def stop(self) -> None:
        if self.pm.isAdvertising():
            self.pm.stopAdvertising()
        self.pm.removeAllServices()
        self.chars.clear()
        self.subscriptions.clear()

    @property
    def clients(self) -> int:
        return len(self.subscriptions)
