"""
puente_usb_rfid.py
===================
Puente para lector USB RFID en COM4.
Lee UIDs del puerto serie, los convierte al formato MFRC522 (SimpleMFRC522._uid_to_num)
y los envía al endpoint /api/rfid/last de la app web.

Uso directo:
    python puente_usb_rfid.py

Modo guardado directo (bypasea el bug del frontend web):
    python puente_usb_rfid.py --save-to-user 9
    python puente_usb_rfid.py --save-to-user 9 --days 7

Como servicio Windows:
    python puente_usb_rfid.py install
    python puente_usb_rfid.py start
    python puente_usb_rfid.py stop
    python puente_usb_rfid.py remove

Dependencias:
    pip install pyserial requests pywin32 python-dotenv
"""

import sys
import os
import time
import re
import logging
from typing import NoReturn
from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE_DIR, "config.env"))

log = logging.getLogger("RFIDBridge")


# ─── Configuración ────────────────────────────────────────────────────
PUERTO_COM = os.getenv("RFID_COM")
if not PUERTO_COM:
    raise ValueError("RFID_COM no configurado en config.env")

BAUD_RATE = int(os.getenv("RFID_BAUD", "9600"))

BASE_URL = os.getenv("RFID_API_URL")
if not BASE_URL:
    raise ValueError("RFID_API_URL no configurado en config.env")

ADMIN_USER = os.getenv("RFID_API_USER")
if not ADMIN_USER:
    raise ValueError("RFID_API_USER no configurado en config.env")

ADMIN_PASS = os.getenv("RFID_API_PASS")
if not ADMIN_PASS:
    raise ValueError("RFID_API_PASS no configurado en config.env")

POLL_INTERVAL = float(os.getenv("RFID_POLL_INTERVAL", "0.1"))
RECONNECT_DELAY = int(os.getenv("RFID_RECONNECT_DELAY", "5"))
DEFAULT_CREDITS = int(os.getenv("RFID_DEFAULT_CREDITS", "10"))
DEFAULT_EXPIRY_DAYS = int(os.getenv("RFID_DEFAULT_EXPIRY_DAYS", "1"))
# ──────────────────────────────────────────────────────────────────────


# ─── Conversión USB → MFRC522 ────────────────────────────────────────
def usb_to_mfrc522(val: int) -> str:
    """Convierte UID decimal del lector USB (little-endian) al formato de
    SimpleMFRC522._uid_to_num (big-endian). Soporta UIDs de 4 y 7 bytes."""
    if val > 0xFFFFFFFFFF:  # 7 bytes
        raw = val.to_bytes(7, "big")  # [b6,b5,b4,b3,b2,b1,b0] en LE
        n = 0
        for b in reversed(raw):  # [b0,b1,b2,b3,b4,b5,b6] en BE
            n = n * 256 + b
        return str(n)
    # 4 bytes (current behavior)
    b0 = (val >> 24) & 0xFF
    b1 = (val >> 16) & 0xFF
    b2 = (val >> 8) & 0xFF
    b3 = val & 0xFF
    bcc = b0 ^ b1 ^ b2 ^ b3
    mfrc_id = (b3 << 32) | (b2 << 24) | (b1 << 16) | (b0 << 8) | bcc
    return str(mfrc_id)


def extraer_uid_usb(data: bytes) -> str | None:
    """Extrae UID del lector USB probando múltiples formatos.
    Todas las estrategias convergen al formato de SimpleMFRC522.uid_to_num
    (4 bytes UID + 1 byte BCC, big-endian)."""

    def raw_bytes_to_mfrc522(raw: bytes) -> str | None:
        """Convierte bytes crudos de UID al formato SimpleMFRC522._uid_to_num.
        - 4 bytes: big-endian + BCC (estándar MiFare Classic)
        - 7 bytes: big-endian directo (NTAG, Ultralight, DESFire)"""
        if len(raw) < 4:
            return None
        if len(raw) >= 7:
            n = 0
            for b in raw[:7]:
                n = n * 256 + b
            return str(n)
        uid_bytes = raw[:4]
        bcc = uid_bytes[0] ^ uid_bytes[1] ^ uid_bytes[2] ^ uid_bytes[3]
        n = 0
        for b in uid_bytes:
            n = n * 256 + b
        n = n * 256 + bcc
        return str(n)

    def try_hex_text(data: bytes) -> str | None:
        """Para lectores que envian el UID como texto hexadecimal (ej: '8EB03102', '0x8EB03102').
        Solo se activa si el texto completo son solo caracteres hex validos."""
        try:
            text = data.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            return None
        cleaned = text.replace("0x", "").replace("0X", "").strip()
        if not re.fullmatch(r"[0-9A-Fa-f]+", cleaned):
            return None
        if not re.search(r"[A-Fa-f]", cleaned):
            return None  # solo digitos → que lo maneje try_usb_text
        if len(cleaned) < 6 or len(cleaned) > 14:
            return None
        if len(cleaned) <= 8:
            raw = bytes.fromhex(cleaned.zfill(8))
        else:
            raw = bytes.fromhex(cleaned.zfill(14))
        return raw_bytes_to_mfrc522(raw)

    def try_usb_text(data: bytes) -> str | None:
        try:
            decoded = data.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            return None
        digits = re.sub(r"[^0-9]", "", decoded)
        if not digits or len(digits) < 4:
            return None
        return usb_to_mfrc522(int(digits))

    def try_esp32(data: bytes) -> str | None:
        try:
            text = data.decode("utf-8", errors="ignore").strip()
        except Exception:
            return None
        match = re.search(r"[0-9]+", text)
        if match and len(match.group()) >= 4:
            return usb_to_mfrc522(int(match.group()))
        return None

    def try_raw_bytes(data: bytes) -> str | None:
        """Convierte bytes crudos a formato MFRC522 (7 o 4 bytes).
        Es la última estrategia — si las de texto fallaron, intenta raw."""
        if len(data) < 4:
            return None
        if len(data) >= 7:
            # 7 bytes → no BCC (NTAG, Ultralight)
            return raw_bytes_to_mfrc522(data)
        # 4 bytes con BCC — solo si NO es texto UTF-8 legible
        try:
            data.decode("utf-8", errors="strict")
            return None  # parece texto, las text-strategies ya fallaron
        except UnicodeDecodeError:
            pass
        return raw_bytes_to_mfrc522(data)

    for strategy in (try_hex_text, try_usb_text, try_raw_bytes, try_esp32):
        result = strategy(data)
        if result:
            return result
    return None


# ─── API ──────────────────────────────────────────────────────────────
def obtener_token() -> str | None:
    import requests
    try:
        r = requests.post(
            f"{BASE_URL}/api/login",
            data={"username": ADMIN_USER, "password": ADMIN_PASS},
            timeout=5,
        )
        if r.status_code == 200:
            log.info("Token JWT obtenido")
            return r.json().get("access_token")
        log.warning(f"Login fallido (HTTP {r.status_code})")
    except requests.exceptions.ConnectionError:
        log.warning(f"Sin conexión con {BASE_URL}")
    except Exception as e:
        log.warning(f"Error en login: {e}")
    return None


def enviar_uid(uid: str, token: str) -> bool:
    import requests
    try:
        r = requests.post(
            f"{BASE_URL}/api/rfid/last",
            json={"uid": uid},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if r.status_code == 200:
            log.info(f"📡 UID enviado: {uid}")
            return True
        elif r.status_code == 401:
            log.info("🔑 Token expirado")
            return False  # señal para refrescar token
        log.warning(f"HTTP {r.status_code}: {r.text}")
    except requests.exceptions.ConnectionError:
        log.warning(f"Sin conexión con {BASE_URL}")
    except Exception as e:
        log.warning(f"Error al enviar UID: {e}")
    return False


# ─── Guardado directo (bypasea bug del frontend) ──────────────────────
def guardar_uid_en_usuario(uid: str, user_id: int, credits: int = DEFAULT_CREDITS, expiry_days: int = DEFAULT_EXPIRY_DAYS) -> bool:
    """Guarda una tarjeta (UID) directamente en un usuario vía /ui/accounts/create.
    By-pasea el bug del frontend donde Alpine.js limpia el UID antes de enviar el form."""
    import requests
    log.info(f"🔐 Iniciando sesión web para guardar UID {uid} en usuario #{user_id}...")
    s = requests.Session()
    try:
        r = s.post(f"{BASE_URL}/ui/login", data={"password": ADMIN_PASS}, timeout=5, allow_redirects=False)
        if r.status_code != 303:
            log.error(f"Login web fallido (HTTP {r.status_code})")
            return False
    except Exception as e:
        log.error(f"Error en login web: {e}")
        return False

    from datetime import datetime, timedelta
    expiry = (datetime.now() + timedelta(days=expiry_days)).strftime("%Y-%m-%dT%H:%M")

    try:
        r = s.post(
            f"{BASE_URL}/ui/accounts/create",
            data={
                "user_id": str(user_id),
                "account_id": uid,
                "status": "active",
                "credits": str(credits),
                "expiration_date": expiry,
            },
            timeout=5,
            allow_redirects=False,
        )
        if r.status_code == 303:
            log.info(f"✅ Tarjeta {uid} guardada en usuario #{user_id}")
            return True
        elif r.status_code == 422:
            log.error(f"Error de validación: {r.text}")
        else:
            log.warning(f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"Error al guardar tarjeta: {e}")
    return False


# ─── Lector serie ─────────────────────────────────────────────────────
class SerialReader:
    def __init__(self, port: str, baud: int):
        self.port = port
        self.baud = baud
        self.ser = None

    def conectar(self) -> bool:
        import serial
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
            self.ser = serial.Serial(self.port, self.baud, timeout=1)
            self.ser.timeout = 0
            self.ser.reset_input_buffer()
            log.info(f"✅ Conectado a {self.port} @ {self.baud} baud")
            return True
        except Exception as e:
            log.warning(f"❌ Error conectando a {self.port}: {e}")
            self.ser = None
            return False

    def leer(self) -> str | None:
        if not self.ser or not self.ser.is_open:
            return None
        try:
            data = self.ser.read(100)
            if data:
                log.debug(f"📥 Raw ({len(data)} bytes): {data.hex()} repr={data!r}")
                uid = extraer_uid_usb(data)
                if uid:
                    log.info(f"✅ UID parseado: {uid}")
                else:
                    log.warning(f"⚠️ No se pudo extraer UID de: {data.hex()} repr={data!r}")
                return uid
        except Exception as e:
            log.warning(f"Error de lectura: {e}")
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        return None

    def cerrar(self):
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass


# ─── Bucle principal ──────────────────────────────────────────────────
def run_forever() -> NoReturn:
    reader = SerialReader(PUERTO_COM, BAUD_RATE)
    token = None
    ultimo_uid_enviado: str | None = None
    ultimo_uid_tiempo = 0.0
    token_obtenido_en = 0.0
    DEDUP_TIMEOUT = 2.0
    TOKEN_TTL = 3300

    while True:
        now = time.time()

        # Reconectar serial si es necesario
        if reader.ser is None:
            reader.conectar()
            if reader.ser is None:
                time.sleep(RECONNECT_DELAY)
                continue
            token = None

        # Obtener/renovar token proactivamente
        if not token or (now - token_obtenido_en) > TOKEN_TTL:
            token = obtener_token()
            token_obtenido_en = time.time()
            if not token:
                time.sleep(RECONNECT_DELAY)
                continue
            log.info(f"Puente activo — {PUERTO_COM} → {BASE_URL}")

        # Limpiar dedup si pasó el timeout de inactividad
        if now - ultimo_uid_tiempo > DEDUP_TIMEOUT:
            ultimo_uid_enviado = None

        # Leer tarjeta
        uid = reader.leer()
        if uid and uid != ultimo_uid_enviado:
            ultimo_uid_enviado = uid
            ultimo_uid_tiempo = time.time()
            ok = enviar_uid(uid, token)
            if not ok:
                token = None

        time.sleep(POLL_INTERVAL)


# ─── Diagnóstico ──────────────────────────────────────────────────────
def diagnose():
    """Lee UNA tarjeta y muestra el raw y todas las conversiones posibles."""
    reader = SerialReader(PUERTO_COM, BAUD_RATE)
    reader.conectar()
    if reader.ser is None:
        log.error(f"No se pudo conectar a {PUERTO_COM}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f" DIAGNÓSTICO — Lector USB en {PUERTO_COM} @ {BAUD_RATE} baud")
    print(f" {'='*60}")
    print(f" Acercá una tarjeta al lector USB...\n")

    while True:
        if not reader.ser or not reader.ser.is_open:
            log.error("Conexión perdida")
            sys.exit(1)
        try:
            data = reader.ser.read(100)
            if not data:
                time.sleep(0.2)
                continue

            print(f"📥 RAW ({len(data)} bytes):")
            print(f"   Hex:       {data.hex().upper()}")
            print(f"   Decimal:   {int.from_bytes(data, 'big')}")
            print(f"   Little-endian decimal: {int.from_bytes(data, 'little')}")
            print(f"   Repr:      {data!r}")
            print(f"   UTF-8:     {data.decode('utf-8', errors='replace')!r}")
            print()

            # Mostrar todos los formatos de UID posibles
            texto = data.decode("utf-8", errors="replace").strip()
            solo_digitos = re.sub(r"[^0-9]", "", texto)

            print("── Posibles UIDs ──")

            if solo_digitos and len(solo_digitos) >= 4:
                val = int(solo_digitos)
                print(f" Decimal crudo:     {val}")
                print(f" Hex crudo:         0x{val:08X}" if val <= 0xFFFFFFFF else f" Hex crudo:         0x{val:014X}")
                uid_actual = usb_to_mfrc522(val)

                if val <= 0xFFFFFFFF:
                    b0 = (val >> 24) & 0xFF
                    b1 = (val >> 16) & 0xFF
                    b2 = (val >> 8) & 0xFF
                    b3 = val & 0xFF
                    bcc = b0 ^ b1 ^ b2 ^ b3
                    # MFRC522 anticollision devuelve bytes al revés: [b3,b2,b1,b0,BCC]
                    # y SimpleMFRC522.uid_to_num los trata como big-endian
                    mfrc522 = (b3 << 32) | (b2 << 24) | (b1 << 16) | (b0 << 8) | bcc
                    print(f" ▶ puente ACTUAL:    {uid_actual}  ← se envía al API")
                    print(f" ▶ MFRC522 espera:   {mfrc522}")
                    if str(mfrc522) == uid_actual:
                        print(f" ✅ Coinciden — conversión CORRECTA")
                    else:
                        print(f" ❌ DIFERENTES")

            uid_final = extraer_uid_usb(data)
            print(f"\n ▶ extraer_uid_usb:  {uid_final}  ← el que se envía al API ahora")

            print(f"\n{'='*60}")
            print(f" MOSTRÁ ESTA MISMA TARJETA en el MFRC522 del RPi")
            print(f" y compará el UID que aparece en el log del RPi")
            print(f" con el 'SimpleMFRC522' de arriba.")
            print(f"{'='*60}")

            reader.cerrar()
            return
        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(0.2)


# ─── Modo guardado directo ────────────────────────────────────────────
def save_to_user_once(user_id: int, days: int = DEFAULT_EXPIRY_DAYS) -> None:
    """Lee UNA tarjeta y la guarda directamente en el usuario indicado."""
    log.info(f"📡 Modo guardado directo — esperando tarjeta para usuario #{user_id}...")
    reader = SerialReader(PUERTO_COM, BAUD_RATE)
    reader.conectar()
    if reader.ser is None:
        log.error(f"No se pudo conectar a {PUERTO_COM}")
        sys.exit(1)

    while True:
        uid = reader.leer()
        if uid:
            log.info(f"✅ Tarjeta leída: {uid}")
            ok = guardar_uid_en_usuario(uid, user_id, expiry_days=days)
            if ok:
                log.info("🎉 Tarjeta guardada exitosamente. Podés cerrar esta ventana.")
            else:
                log.error("❌ No se pudo guardar la tarjeta. Revisá los logs.")
            reader.cerrar()
            return
        time.sleep(0.2)


# ─── Servicio Windows ─────────────────────────────────────────────────
def is_service() -> bool:
    return "--service" in sys.argv


def service_main():
    """Ejecuta el bucle principal silenciosamente (sin stdout)."""
    logging.getLogger().handlers.clear()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        filename=os.path.join(_BASE_DIR, "puente_usb_rfid.log"),
    )
    run_forever()


# ─── Entry point ──────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if is_service():
        service_main()
    elif sys.argv[1] == "--save-to-user":
        user_id = None
        days = DEFAULT_EXPIRY_DAYS
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--days":
                i += 1
                if i < len(sys.argv):
                    days = int(sys.argv[i])
            else:
                try:
                    user_id = int(sys.argv[i])
                except ValueError:
                    print(f"Argumento inválido: {sys.argv[i]}")
                    sys.exit(1)
            i += 1
        if user_id is None:
            user_id = int(os.getenv("RFID_DEFAULT_USER_ID", "0"))
        if user_id <= 0:
            print("Uso: python puente_usb_rfid.py --save-to-user <ID_USUARIO> [--days N]")
            print("O configurá RFID_DEFAULT_USER_ID en config.env")
            sys.exit(1)
        save_to_user_once(user_id, days)
    elif "--save-to-user" in sys.argv:
        user_id = int(os.getenv("RFID_DEFAULT_USER_ID", "0"))
        if user_id <= 0:
            print("Especificá el ID de usuario: python puente_usb_rfid.py --save-to-user <ID>")
            print("O configurá RFID_DEFAULT_USER_ID en config.env")
            sys.exit(1)
        save_to_user_once(user_id)
    elif "--diagnose" in sys.argv or "-d" in sys.argv:
        diagnose()
    else:
        try:
            log.info("=== Puente USB RFID — Ctrl+C para salir ===")
            run_forever()
        except KeyboardInterrupt:
            log.info("Puente cerrado.")
