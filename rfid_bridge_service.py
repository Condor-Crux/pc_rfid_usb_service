"""
rfid_bridge_service.py
======================
Servicio Windows para el puente USB RFID.
Se ejecuta automáticamente al iniciar Windows (en segundo plano).

Instalación:
    pip install pyserial requests pywin32

    python rfid_bridge_service.py install
    python rfid_bridge_service.py start
    python rfid_bridge_service.py stop
    python rfid_bridge_service.py remove

Ver logs en: %TEMP%\rfid_bridge_service.log
"""

import os
import time
import logging
import threading
import tempfile

import win32serviceutil
import win32service
import win32event

SERVICE_NAME = "RFIDBridgeService"
SERVICE_DISPLAY_NAME = "Puente USB RFID - RPi Door Access"


class RFIDBridgeService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY_NAME
    _svc_description_ = "Lee tarjetas RFID del lector USB (COM4) y las envía a la API web de control de acceso."

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self._running = False
        self._thread = None
        self.log = None

    def SvcDoRun(self):
        log_file = os.path.join(tempfile.gettempdir(), "rfid_bridge_service.log")
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(fh)
        logging.getLogger().setLevel(logging.INFO)

        self.log = logging.getLogger("RFIDBridgeSvc")
        self.log.info(f"Servicio iniciado. Log: {log_file}")

        self._running = True
        self._thread = threading.Thread(target=self._run_bridge, daemon=True)
        self._thread.start()
        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)
        self.log.info("Servicio detenido.")

    def SvcStop(self):
        if self.log:
            self.log.info("Deteniendo servicio...")
        self._running = False
        win32event.SetEvent(self.hWaitStop)

    def _run_bridge(self):
        from puente_usb_rfid import SerialReader, obtener_token, enviar_uid, PUERTO_COM, BAUD_RATE, BASE_URL, RECONNECT_DELAY, POLL_INTERVAL

        reader = SerialReader(PUERTO_COM, BAUD_RATE)
        token = None
        ultimo_uid_enviado = None
        ultimo_uid_tiempo = 0.0
        token_obtenido_en = 0.0
        DEDUP_TIMEOUT = 2.0
        TOKEN_TTL = 3300

        while self._running:
            try:
                now = time.time()

                if reader.ser is None:
                    reader.conectar()
                    if reader.ser is None:
                        time.sleep(RECONNECT_DELAY)
                        continue
                    token = None

                if not token or (now - token_obtenido_en) > TOKEN_TTL:
                    token = obtener_token()
                    token_obtenido_en = time.time()
                    if not token:
                        time.sleep(RECONNECT_DELAY)
                        continue
                    self.log.info(f"Puente activo — {PUERTO_COM} → {BASE_URL}")

                now = time.time()
                if now - ultimo_uid_tiempo > DEDUP_TIMEOUT:
                    ultimo_uid_enviado = None

                uid = reader.leer()
                if uid and uid != ultimo_uid_enviado:
                    ultimo_uid_enviado = uid
                    ultimo_uid_tiempo = time.time()
                    ok = enviar_uid(uid, token)
                    if not ok:
                        token = None

                time.sleep(POLL_INTERVAL)

            except Exception as e:
                if self.log:
                    self.log.error(f"Error en bucle: {e}")
                time.sleep(RECONNECT_DELAY)

        reader.cerrar()
        if self.log:
            self.log.info("Bucle finalizado.")


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(RFIDBridgeService)
