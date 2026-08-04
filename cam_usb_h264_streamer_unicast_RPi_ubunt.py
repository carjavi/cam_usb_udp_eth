#!/usr/bin/env python3
"""
Streaming en tiempo real de una camara USB con H264 nativo hacia la red via RTP/UDP,
compatible con QGroundControl y VLC (mismo estandar que BlueOS / mavlink-camera-manager).

@author: Carlos Briceno <carjavi@hotmail.com>
@date: 21-07-2026
@copyright: Copyright (c) 2026 www.carjavi.com
@version: V1.1
@library:
- Instalacion con un unico script (ver install.sh): instala los paquetes de
  sistema (GStreamer, v4l-utils, headers) y PyGObject dentro del venv,
  compilado especificamente para su interprete.
    ./install.sh

Uso:
    ./cam_usb_h264_streamer_unicast.py <ip_cliente_1> [ip_cliente_2 ...]

Decisiones tecnicas relevantes:
- No se usa asyncio: GLib.MainLoop ya es el bucle de eventos nativo de GStreamer y
  cubre por completo las necesidades de este script (un unico flujo secuencial de
  captura -> pipeline -> reintento). Combinarlo con asyncio anadiria complejidad sin
  beneficio real.
- Logging con formato timestamp/nivel/modulo en texto plano (no JSON): este script se
  ejecuta manualmente en una terminal y debe ser legible en tiempo real por el
  operador; no hay un agregador de logs en este modo de despliegue.
- Destino UDP = UNICAST a los clientes indicados por argumento (via multiudpsink,
  igual que mavlink-camera-manager/BlueOS en src/lib/stream/sink/udp_sink.rs:
  "multiudpsink sync=false clients=ip:puerto,..."), NO la direccion de broadcast del
  subnet. Se cambio de broadcast a unicast tras confirmar con ffprobe (conteo de
  gaps en el numero de secuencia RTP) que, a traves de un enlace HomePlug AV/PLC
  (LX200V20/Fathom), el trafico broadcast no tiene reintento/ACK a nivel de capa MAC
  (igual que en WiFi: solo el trafico unicast se retransmite ante error del medio),
  lo que produce perdida de paquetes real y medible (~9 gaps/seg) que el broadcast
  nunca recupera. Con el mismo enlace y la misma camara, unicast midio 0 gaps en la
  misma ventana de prueba. La IP propia se sigue usando en el campo c= del SDP
  informativo, tal como en el descriptor de referencia.
- La deteccion de camara usa el binario v4l2-ctl (paquete v4l-utils) via subprocess,
  NO gst-launch-1.0: el requisito de evitar subprocess aplica al pipeline de streaming
  (para tener control programatico de errores/reconexion via el bus de GStreamer), no a
  la fase de enumeracion/verificacion de formatos soportados por el hardware, donde
  v4l2-ctl es la herramienta estandar y ya es una dependencia de sistema documentada.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid
from types import FrameType
from typing import Callable

try:
    import fcntl
except ImportError as import_error:
    sys.stderr.write(
        "Error: este script solo puede ejecutarse en Linux (requiere el modulo 'fcntl').\n"
        f"Detalle: {import_error}\n"
    )
    sys.exit(1)

try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib
except (ImportError, ValueError) as import_error:
    sys.stderr.write(
        "Error: no se encontraron los bindings de GStreamer (PyGObject/Gst).\n"
        "Instale las dependencias con:\n"
        "  ./install.sh\n"
        f"Detalle: {import_error}\n"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constantes de configuracion
# ---------------------------------------------------------------------------

APP_NAME = "usb_h264_streamer"
"""Nombre de la aplicacion, usado en logs y en el atributo a=tool del SDP informativo."""

APP_VERSION = "V1.1"
"""Version de la aplicacion, usada en logs y en el atributo a=tool del SDP informativo."""

STREAM_WIDTH = 1280
"""Ancho de video requerido en la camara USB (pixeles)."""

STREAM_HEIGHT = 720
"""Alto de video requerido en la camara USB (pixeles)."""

STREAM_FRAMERATE = 30
"""Framerate requerido en la camara USB (fps)."""

STREAM_UDP_PORT = 5600
"""Puerto UDP de destino, fijo por compatibilidad con QGroundControl/BlueOS."""

RTP_PAYLOAD_TYPE = 96
"""Payload type RTP dinamico usado para H264, segun RFC 3551 (96-127)."""

RECONNECT_DELAY_SECONDS = 4
"""Tiempo de espera entre reintentos de deteccion/streaming tras una falla."""

DEVICE_POLL_INTERVAL_SECONDS = 1
"""Intervalo de verificacion de presencia fisica del dispositivo /dev/videoX durante el streaming."""

PLAYING_TIMEOUT_SECONDS = 8
"""Tiempo maximo de espera a que el pipeline confirme PLAYING antes de abortar y reintentar.

Cubre el caso de una negociacion de caps que se cuelga indefinidamente sin emitir
ningun mensaje de ERROR en el bus (p. ej. un stream-format que la camara no soporta):
sin este watchdog, el mainloop se queda esperando para siempre sin dar ninguna senal."""

V4L2_CTL_TIMEOUT_SECONDS = 5
"""Timeout maximo para cada invocacion del binario v4l2-ctl."""

CAMERA_GLOB_PATTERN = "/dev/video*"
"""Patron glob usado para enumerar los nodos de dispositivo de video V4L2."""

SDP_OUTPUT_PATH = "stream_reference.sdp"
"""Ruta donde se escribe el descriptor SDP informativo, generado dinamicamente desde el stream real."""

SIOCGIFADDR = 0x8915
"""Codigo ioctl de Linux para obtener la direccion IPv4 de una interfaz de red."""

EXCLUDED_INTERFACE_PREFIXES = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap")
"""Prefijos de interfaces virtuales/loopback a excluir al buscar la interfaz Ethernet activa."""

LOG_FILE_PATH = "usb_h264_streamer.log"
"""Archivo donde se registra el detalle tecnico (misma logica que BlueOS/mavlink-camera-manager,
que dejan el detalle en el log del servicio y muestran solo el estado al operador)."""

STATUS_RUNNING = "running!"
"""Texto de estado mostrado en consola mientras el streaming esta activo."""

STATUS_CANCELLED = "cancelado"
"""Texto de estado mostrado en consola al cerrar la aplicacion (Ctrl+C/SIGTERM)."""

logger = logging.getLogger(APP_NAME)


def configure_logging() -> None:
    """Configura el logging raiz con salida a archivo en formato timestamp/nivel/modulo."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        filename=LOG_FILE_PATH,
    )


def print_status(status: str) -> None:
    """Imprime en consola la unica linea de estado visible para el operador."""
    print(f"status: {status}", flush=True)


def print_video_target(clients: list[str], port: int) -> None:
    """Imprime la linea de destino del stream, justo antes de la linea de estado."""
    destinos = ", ".join(f"udp://{ip}:{port}" for ip in clients)
    print(f"video to {destinos}", flush=True)


# ---------------------------------------------------------------------------
# Deteccion de camara USB (V4L2)
# ---------------------------------------------------------------------------


class CameraDetector:
    """Detecta automaticamente la primera camara USB que entrega H264 nativo
    en la resolucion y framerate configurados (sin recodificar)."""

    def list_video_devices(self) -> list[str]:
        return sorted(glob.glob(CAMERA_GLOB_PATTERN))

    def _run_v4l2_ctl(self, args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["v4l2-ctl", *args],
                capture_output=True,
                text=True,
                timeout=V4L2_CTL_TIMEOUT_SECONDS,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as error:
            logger.debug("Fallo al ejecutar 'v4l2-ctl %s': %s", " ".join(args), error)
            return None
        return result.stdout

    def _get_bus_info(self, device_path: str) -> str | None:
        output = self._run_v4l2_ctl(["-d", device_path, "-D"])
        if not output:
            return None
        match = re.search(r"Bus info\s*:\s*(\S+)", output)
        return match.group(1) if match else None

    def _supports_target_format(self, device_path: str) -> bool:
        output = self._run_v4l2_ctl(["-d", device_path, "--list-formats-ext"])
        if not output:
            return False

        current_format: str | None = None
        current_size: tuple[int, int] | None = None

        for line in output.splitlines():
            format_match = re.match(r"\s*\[\d+\]:\s*'(\w+)'", line)
            if format_match:
                current_format = format_match.group(1)
                continue

            size_match = re.match(r"\s*Size:\s*Discrete\s*(\d+)x(\d+)", line)
            if size_match:
                current_size = (int(size_match.group(1)), int(size_match.group(2)))
                continue

            fps_match = re.search(r"\(([\d.]+)\s*fps\)", line)
            if (
                fps_match
                and current_format == "H264"
                and current_size == (STREAM_WIDTH, STREAM_HEIGHT)
                and abs(float(fps_match.group(1)) - STREAM_FRAMERATE) < 0.5
            ):
                return True

        return False

    def find_h264_camera(self) -> str | None:
        for device_path in self.list_video_devices():
            bus_info = self._get_bus_info(device_path)
            if not bus_info or "usb" not in bus_info.lower():
                logger.debug(
                    "%s descartado: no es un dispositivo USB (bus_info=%s)", device_path, bus_info
                )
                continue
            if not self._supports_target_format(device_path):
                logger.debug(
                    "%s descartado: no soporta H264 %dx%d@%dfps",
                    device_path, STREAM_WIDTH, STREAM_HEIGHT, STREAM_FRAMERATE,
                )
                continue
            logger.debug("%s aceptado: USB + H264 %dx%d@%dfps", device_path, STREAM_WIDTH, STREAM_HEIGHT, STREAM_FRAMERATE)
            return device_path
        return None


# ---------------------------------------------------------------------------
# Deteccion de interfaz Ethernet activa e IP (solo para el campo c= informativo del SDP)
# ---------------------------------------------------------------------------


class NetworkDetector:
    """Detecta la interfaz Ethernet activa del equipo y su IP propia, usada solo
    para el campo informativo c= del SDP de referencia (no para el destino real
    del stream, que ahora es unicast explicito via argumentos de linea de comando)."""

    @staticmethod
    def list_ethernet_interfaces() -> list[str]:
        interfaces: list[str] = []
        net_class_path = "/sys/class/net"
        if not os.path.isdir(net_class_path):
            return interfaces

        for iface in sorted(os.listdir(net_class_path)):
            if iface.startswith(EXCLUDED_INTERFACE_PREFIXES):
                continue
            if os.path.isdir(os.path.join(net_class_path, iface, "wireless")):
                continue

            type_path = os.path.join(net_class_path, iface, "type")
            operstate_path = os.path.join(net_class_path, iface, "operstate")
            try:
                with open(type_path, encoding="utf-8") as type_file:
                    iface_type = type_file.read().strip()
                with open(operstate_path, encoding="utf-8") as operstate_file:
                    operstate = operstate_file.read().strip()
            except OSError:
                continue

            if iface_type == "1" and operstate == "up":
                interfaces.append(iface)

        return interfaces

    @staticmethod
    def get_interface_ipv4(ifname: str) -> str | None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                packed_ifname = struct.pack("256s", ifname.encode("utf-8")[:15])
                raw_address = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, packed_ifname)
                return socket.inet_ntoa(raw_address[20:24])
            except OSError:
                return None

    def find_active_ethernet(self) -> tuple[str, str] | None:
        """Busca la primera interfaz Ethernet activa con IPv4 asignada.

        Returns:
            Tupla (interfaz, ip_propia) o None si ninguna interfaz Ethernet
            activa tiene una IPv4 asignada.
        """
        for iface in self.list_ethernet_interfaces():
            ip_address = self.get_interface_ipv4(iface)
            if ip_address:
                return iface, ip_address
        return None


# ---------------------------------------------------------------------------
# Pipeline de GStreamer
# ---------------------------------------------------------------------------


class GstStreamPipeline:
    """Construye y administra el pipeline de GStreamer que remuxea el H264
    nativo de la camara a paquetes RTP/UDP unicast, usando bindings PyGObject
    (sin invocar gst-launch-1.0 como subproceso)."""

    def __init__(
        self,
        device_path: str,
        own_ip: str,
        client_ips: list[str],
        on_playing: Callable[[], None] | None = None,
    ) -> None:
        """Inicializa el manejador de pipeline para un dispositivo y destinos dados.

        Args:
            device_path: Nodo V4L2 de la camara, por ejemplo "/dev/video0".
            own_ip: IP propia del equipo, usada solo para el campo c= del SDP informativo.
            client_ips: Lista de IPs unicast de los clientes a los que se envia el
                stream (equivalente al "clients=ip:puerto,..." de multiudpsink que
                usa mavlink-camera-manager/BlueOS).
            on_playing: Callback invocado una vez cuando el pipeline confirma (via el bus)
                que efectivamente alcanzo el estado PLAYING.
        """
        self.device_path = device_path
        self.own_ip = own_ip
        self.client_ips = client_ips
        self._on_playing = on_playing
        self.pipeline: Gst.Pipeline | None = None
        self._mainloop: GLib.MainLoop | None = None
        self._stop_reason: str | None = None
        self._reached_playing = False
        self._playing_deadline: float | None = None

    def _make_element(self, factory_name: str, element_name: str) -> Gst.Element:
        element = Gst.ElementFactory.make(factory_name, element_name)
        if element is None:
            raise RuntimeError(
                f"No se pudo crear el elemento GStreamer '{factory_name}'. Verifique que los "
                "plugins de GStreamer esten instalados (gstreamer1.0-plugins-base/good/bad)."
            )
        return element

    def build(self) -> None:
        """Construye el pipeline: v4l2src -> queue -> capsfilter(H264) -> h264parse ->
        rtph264pay -> multiudpsink, sin recodificar el video en ningun punto.

        Raises:
            RuntimeError: Si algun elemento de GStreamer requerido no puede crearse.
        """
        self.pipeline = Gst.Pipeline.new("usb_h264_streamer_pipeline")

        source = self._make_element("v4l2src", "camera_source")
        source.set_property("device", self.device_path)
        source.set_property("do-timestamp", True)

        queue = self._make_element("queue", "capture_queue")

        caps_filter = self._make_element("capsfilter", "camera_caps")
        caps_string = (
            f"video/x-h264,width={STREAM_WIDTH},height={STREAM_HEIGHT},"
            f"framerate={STREAM_FRAMERATE}/1"
        )
        caps_filter.set_property("caps", Gst.Caps.from_string(caps_string))

        parser = self._make_element("h264parse", "h264_parser")
        parser.set_property("config-interval", -1)

        payloader = self._make_element("rtph264pay", "rtp_payloader")
        payloader.set_property("pt", RTP_PAYLOAD_TYPE)
        payloader.set_property("config-interval", -1)
        try:
            payloader.set_property("aggregate-mode", "zero-latency")
        except TypeError:
            logger.debug("rtph264pay no soporta 'aggregate-mode' en esta version de GStreamer.")

        # multiudpsink con "clients=ip:puerto,..." en vez de udpsink a la
        # direccion de broadcast: igual que mavlink-camera-manager/BlueOS
        # (src/lib/stream/sink/udp_sink.rs). No requiere SO_BROADCAST porque
        # cada paquete va dirigido a una IP unicast especifica, lo que en un
        # enlace HomePlug AV/PLC (LX200V20) si se beneficia del
        # reintento/ACK a nivel de capa MAC (el broadcast no).
        clients = ",".join(f"{ip}:{STREAM_UDP_PORT}" for ip in self.client_ips)
        sink = self._make_element("multiudpsink", "udp_sink")
        sink.set_property("clients", clients)
        sink.set_property("sync", False)

        for element in (source, queue, caps_filter, parser, payloader, sink):
            self.pipeline.add(element)

        source.link(queue)
        queue.link(caps_filter)
        caps_filter.link(parser)
        parser.link(payloader)
        payloader.link(sink)

        rtp_src_pad = payloader.get_static_pad("src")
        rtp_src_pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_rtp_caps_probe)

    def _on_rtp_caps_probe(self, pad: Gst.Pad, probe_info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        event = probe_info.get_event()
        if event is None or event.type != Gst.EventType.CAPS:
            return Gst.PadProbeReturn.OK

        caps = event.parse_caps()
        structure = caps.get_structure(0)
        sprop_parameter_sets = structure.get_string("sprop-parameter-sets")
        profile_level_id = structure.get_string("profile-level-id")

        if sprop_parameter_sets and profile_level_id:
            self._log_sdp_reference(sprop_parameter_sets, profile_level_id)
            return Gst.PadProbeReturn.REMOVE

        return Gst.PadProbeReturn.OK

    def _log_sdp_reference(self, sprop_parameter_sets: str, profile_level_id: str) -> None:
        session_id = uuid.uuid4()
        sdp_text = (
            "v=0\n"
            f"s={session_id}\n"
            "i=This is a UDP stream\n"
            "t=0 0\n"
            f"a=tool:{APP_NAME} - {APP_VERSION}\n"
            "a=type:broadcast\n"
            "a=recvonly\n"
            f"m=video {STREAM_UDP_PORT} RTP/AVP {RTP_PAYLOAD_TYPE}\n"
            f"c=IN IP4 {self.own_ip}\n"
            f"a=rtpmap:{RTP_PAYLOAD_TYPE} H264/90000\n"
            f"a=framerate:{STREAM_FRAMERATE}\n"
            f"a=fmtp:{RTP_PAYLOAD_TYPE} packetization-mode=1;"
            f"sprop-parameter-sets={sprop_parameter_sets};"
            f"profile-level-id={profile_level_id};level-asymmetry-allowed=1\n"
        )
        logger.info("Descriptor SDP generado dinamicamente desde el stream real:\n%s", sdp_text)
        try:
            with open(SDP_OUTPUT_PATH, "w", encoding="utf-8") as sdp_file:
                sdp_file.write(sdp_text)
            logger.info("SDP de referencia escrito en %s", SDP_OUTPUT_PATH)
        except OSError as error:
            logger.warning("No se pudo escribir el archivo SDP de referencia: %s", error)

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message) -> bool:
        message_type = message.type
        if message_type == Gst.MessageType.ERROR:
            error, debug_info = message.parse_error()
            logger.error("Error de GStreamer: %s (%s)", error.message, debug_info)
            self._stop_reason = "error"
            if self._mainloop:
                self._mainloop.quit()
        elif message_type == Gst.MessageType.EOS:
            logger.warning("Fin de stream (EOS) recibido desde el pipeline.")
            self._stop_reason = "eos"
            if self._mainloop:
                self._mainloop.quit()
        elif message_type == Gst.MessageType.WARNING:
            warning, debug_info = message.parse_warning()
            logger.warning("Advertencia de GStreamer: %s (%s)", warning.message, debug_info)
        elif message_type == Gst.MessageType.STATE_CHANGED and message.src == self.pipeline:
            _, new_state, _ = message.parse_state_changed()
            if new_state == Gst.State.PLAYING:
                self._reached_playing = True
                if self._on_playing is not None:
                    self._on_playing()
                    self._on_playing = None
        return True

    def _check_device_present(self) -> bool:
        if not os.path.exists(self.device_path):
            logger.error("El dispositivo de camara %s ya no esta presente.", self.device_path)
            self._stop_reason = "device_lost"
            if self._mainloop:
                self._mainloop.quit()
            return True

        if (
            not self._reached_playing
            and self._playing_deadline is not None
            and time.monotonic() >= self._playing_deadline
        ):
            logger.error(
                "El pipeline no confirmo PLAYING tras %d s (probable cuelgue de "
                "negociacion de caps). Abortando para reintentar.",
                PLAYING_TIMEOUT_SECONDS,
            )
            self._stop_reason = "playing_timeout"
            if self._mainloop:
                self._mainloop.quit()
        return True

    def run(self) -> str | None:
        assert self.pipeline is not None, "build() debe llamarse antes de run()"

        self._mainloop = GLib.MainLoop()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus_handler_id = bus.connect("message", self._on_bus_message)
        device_check_id = GLib.timeout_add_seconds(
            DEVICE_POLL_INTERVAL_SECONDS, self._check_device_present
        )

        self._playing_deadline = time.monotonic() + PLAYING_TIMEOUT_SECONDS
        state_change_return = self.pipeline.set_state(Gst.State.PLAYING)
        if state_change_return == Gst.StateChangeReturn.FAILURE:
            GLib.source_remove(device_check_id)
            bus.disconnect(bus_handler_id)
            bus.remove_signal_watch()
            raise RuntimeError(
                "GStreamer rechazo el cambio de estado a PLAYING (fallo sincronico); "
                "revise las propiedades/caps del pipeline."
            )
        logger.info(
            "Streaming solicitado: dispositivo=%s destino=%s (unicast, pt=%d)",
            self.device_path,
            ", ".join(f"udp://{ip}:{STREAM_UDP_PORT}" for ip in self.client_ips),
            RTP_PAYLOAD_TYPE,
        )

        try:
            self._mainloop.run()
        finally:
            GLib.source_remove(device_check_id)
            bus.disconnect(bus_handler_id)
            bus.remove_signal_watch()

        return self._stop_reason

    def quit(self) -> None:
        self._stop_reason = "shutdown_requested"
        if self._mainloop and self._mainloop.is_running():
            self._mainloop.quit()

    def stop(self) -> None:
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None


# ---------------------------------------------------------------------------
# Aplicacion principal
# ---------------------------------------------------------------------------


class StreamerApplication:
    """Orquesta el ciclo completo: deteccion de camara, deteccion de red,
    construccion/ejecucion del pipeline y reconexion automatica ante fallas."""

    def __init__(self, client_ips: list[str]) -> None:
        self.client_ips = client_ips
        self._shutdown_requested = False
        self._current_pipeline: GstStreamPipeline | None = None
        signal.signal(signal.SIGINT, self._handle_shutdown_signal)
        signal.signal(signal.SIGTERM, self._handle_shutdown_signal)

    def _handle_shutdown_signal(self, signum: int, frame: FrameType | None) -> None:
        logger.info("Senal de interrupcion recibida (signum=%d), cerrando de forma limpia...", signum)
        self._shutdown_requested = True
        if self._current_pipeline is not None:
            self._current_pipeline.quit()

    def _wait_for_camera(self) -> str | None:
        camera_detector = CameraDetector()
        while not self._shutdown_requested:
            device_path = camera_detector.find_h264_camera()
            if device_path:
                return device_path
            logger.warning(
                "No se detecto ninguna camara USB compatible con H264 %dx%d@%dfps. "
                "Reintentando en %d s...",
                STREAM_WIDTH, STREAM_HEIGHT, STREAM_FRAMERATE, RECONNECT_DELAY_SECONDS,
            )
            time.sleep(RECONNECT_DELAY_SECONDS)
        return None

    def _wait_for_network(self) -> tuple[str, str] | None:
        network_detector = NetworkDetector()
        while not self._shutdown_requested:
            network_info = network_detector.find_active_ethernet()
            if network_info:
                return network_info
            logger.warning(
                "No se detecto ninguna interfaz Ethernet activa con IPv4 asignada. "
                "Reintentando en %d s...", RECONNECT_DELAY_SECONDS,
            )
            time.sleep(RECONNECT_DELAY_SECONDS)
        return None

    def run(self) -> None:
        logger.info("Iniciando %s %s (clientes unicast: %s)", APP_NAME, APP_VERSION, ", ".join(self.client_ips))

        while not self._shutdown_requested:
            device_path = self._wait_for_camera()
            if device_path is None:
                break
            logger.info("Camara detectada: %s", device_path)

            network_info = self._wait_for_network()
            if network_info is None:
                break
            iface, own_ip = network_info
            logger.info("Interfaz Ethernet activa: %s (ip=%s)", iface, own_ip)

            def _on_playing(client_ips: list[str] = self.client_ips) -> None:
                print_video_target(client_ips, STREAM_UDP_PORT)
                print_status(STATUS_RUNNING)

            gst_pipeline = GstStreamPipeline(
                device_path, own_ip, self.client_ips, on_playing=_on_playing
            )
            self._current_pipeline = gst_pipeline
            stop_reason: str | None
            try:
                gst_pipeline.build()
                stop_reason = gst_pipeline.run()
            except Exception as error:  # noqa: BLE001 - se registra y se reintenta, no se propaga
                logger.error("Fallo al construir/ejecutar el pipeline: %s", error)
                stop_reason = "exception"
            finally:
                gst_pipeline.stop()
                self._current_pipeline = None

            if self._shutdown_requested:
                break

            logger.warning(
                "Streaming detenido (motivo=%s). Reintentando en %d s...",
                stop_reason, RECONNECT_DELAY_SECONDS,
            )
            time.sleep(RECONNECT_DELAY_SECONDS)

        logger.info("%s finalizado.", APP_NAME)
        print_status(STATUS_CANCELLED)


def check_required_system_tools() -> None:
    if shutil.which("v4l2-ctl") is None:
        logger.error(
            "No se encontro el binario 'v4l2-ctl'. Instalelo con: sudo apt install v4l-utils"
        )
        sys.stderr.write(
            "Error fatal: no se encontro el binario 'v4l2-ctl'. Instalelo con: "
            "sudo apt install v4l-utils\n"
        )
        sys.exit(1)


def main() -> None:
    """Punto de entrada: configura logging, valida dependencias, inicializa GStreamer y arranca la app."""
    if len(sys.argv) < 2:
        sys.stderr.write(
            "Uso: cam_usb_h264_streamer_unicast.py <ip_cliente_1> [ip_cliente_2 ...]\n"
            "Ejemplo: cam_usb_h264_streamer_unicast.py 192.168.1.200\n"
        )
        sys.exit(1)
    client_ips = sys.argv[1:]

    configure_logging()
    check_required_system_tools()
    Gst.init(None)
    app = StreamerApplication(client_ips)
    app.run()


if __name__ == "__main__":
    main()
