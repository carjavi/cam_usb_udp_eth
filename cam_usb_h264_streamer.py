#!/usr/bin/env python3
"""
Streaming en tiempo real de una camara USB con H264 nativo hacia la red via RTP/UDP,
compatible con QGroundControl y VLC (mismo estandar que BlueOS / mavlink-camera-manager).

@author: Carlos Briceno <carjavi@hotmail.com>
@date: 21-07-2026
@copyright: Copyright (c) 2026 www.carjavi.com
@version: V1.0
@library:
- Instalacion con un unico script (ver install.sh): instala los paquetes de
  sistema (GStreamer, v4l-utils, headers) y PyGObject dentro del venv,
  compilado especificamente para su interprete.
    ./install.sh

Decisiones tecnicas relevantes:
- No se usa asyncio: GLib.MainLoop ya es el bucle de eventos nativo de GStreamer y
  cubre por completo las necesidades de este script (un unico flujo secuencial de
  captura -> pipeline -> reintento). Combinarlo con asyncio anadiria complejidad sin
  beneficio real.
- Logging con formato timestamp/nivel/modulo en texto plano (no JSON): este script se
  ejecuta manualmente en una terminal y debe ser legible en tiempo real por el
  operador; no hay un agregador de logs en este modo de despliegue.
- Destino UDP = direccion de broadcast del subnet de la interfaz Ethernet activa
  (IP | ~netmask), NO la IP unicast propia. Enviar paquetes a la propia IP unicast
  los entregaria por loopback y jamas saldrian al cable, incumpliendo el requisito de
  que "cualquier cliente en la misma red" (VLC/QGC en otro equipo) reciba el stream sin
  configuracion adicional. La IP propia se sigue usando en el campo c= del SDP
  informativo, tal como en el descriptor de referencia.
- La deteccion de camara usa el binario v4l2-ctl (paquete v4l-utils) via subprocess,
  NO gst-launch-1.0: el requisito de evitar subprocess aplica al pipeline de streaming
  (para tener control programatico de errores/reconexion via el bus de GStreamer), no a
  la fase de enumeracion/verificacion de formatos soportados por el hardware, donde
  v4l2-ctl es la herramienta estandar y ya es una dependencia de sistema documentada.
"""

from __future__ import annotations

import glob
import ipaddress
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
    gi.require_version("Gio", "2.0")
    from gi.repository import Gst, GLib, Gio
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

APP_VERSION = "V1.0"
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

SIOCGIFNETMASK = 0x891B
"""Codigo ioctl de Linux para obtener la mascara de subred IPv4 de una interfaz de red."""

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
    """Configura el logging raiz con salida a archivo en formato timestamp/nivel/modulo.

    El detalle tecnico no se muestra en consola: el operador solo ve la linea de
    estado ("status: running!"/"status: cancelado") impresa por print_status(),
    igual que en BlueOS/mavlink-camera-manager. El detalle completo queda en
    LOG_FILE_PATH para diagnostico posterior.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        filename=LOG_FILE_PATH,
    )


def print_status(status: str) -> None:
    """Imprime en consola la unica linea de estado visible para el operador."""
    print(f"status: {status}", flush=True)


def print_video_target(iface: str, ip_address: str, port: int) -> None:
    """Imprime la linea de destino del stream, justo antes de la linea de estado."""
    print(f"video to {iface} udp://{ip_address}:{port}", flush=True)


# ---------------------------------------------------------------------------
# Deteccion de camara USB (V4L2)
# ---------------------------------------------------------------------------


class CameraDetector:
    """Detecta automaticamente la primera camara USB que entrega H264 nativo
    en la resolucion y framerate configurados (sin recodificar)."""

    def list_video_devices(self) -> list[str]:
        """Enumera los nodos de dispositivo de video disponibles en el sistema.

        Returns:
            Lista ordenada de rutas /dev/videoX encontradas.
        """
        return sorted(glob.glob(CAMERA_GLOB_PATTERN))

    def _run_v4l2_ctl(self, args: list[str]) -> str | None:
        """Ejecuta v4l2-ctl con timeout y devuelve su stdout, o None si falla.

        Args:
            args: Argumentos adicionales para el binario v4l2-ctl.

        Returns:
            La salida estandar del comando, o None si el binario no existe,
            excede el timeout o termina con un error de ejecucion del proceso.
        """
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
        """Obtiene el campo 'Bus info' de un dispositivo V4L2, usado para confirmar que es USB."""
        output = self._run_v4l2_ctl(["-d", device_path, "-D"])
        if not output:
            return None
        match = re.search(r"Bus info\s*:\s*(\S+)", output)
        return match.group(1) if match else None

    def _supports_target_format(self, device_path: str) -> bool:
        """Verifica si el dispositivo soporta H264 nativo en STREAM_WIDTH x STREAM_HEIGHT @ STREAM_FRAMERATE.

        Parsea la salida de texto de 'v4l2-ctl --list-formats-ext', que agrupa
        formatos de pixel, tamanos discretos e intervalos de captura de forma
        jerarquica e indentada.
        """
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
        """Busca la primera camara USB compatible con H264 nativo en la resolucion/framerate configurados.

        Returns:
            La ruta del dispositivo (por ejemplo "/dev/video0") si se encuentra una
            camara compatible, o None si ninguna cumple los requisitos.
        """
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
# Deteccion de interfaz Ethernet activa e IP
# ---------------------------------------------------------------------------


class NetworkDetector:
    """Detecta la interfaz Ethernet activa del equipo y calcula su IP y su
    direccion de broadcast, sin requerir configuracion manual."""

    @staticmethod
    def list_ethernet_interfaces() -> list[str]:
        """Enumera interfaces Ethernet fisicas activas (excluye loopback, WiFi y virtuales).

        Returns:
            Lista de nombres de interfaz (por ejemplo ["eth0"]) que son de tipo
            Ethernet (ARPHRD_ETHER), no inalambricas y estan en estado "up".
        """
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
        """Obtiene la IPv4 asignada a una interfaz de red usando ioctl SIOCGIFADDR.

        Args:
            ifname: Nombre de la interfaz (por ejemplo "eth0").

        Returns:
            La direccion IPv4 en formato texto, o None si la interfaz no tiene IP asignada.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                packed_ifname = struct.pack("256s", ifname.encode("utf-8")[:15])
                raw_address = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, packed_ifname)
                return socket.inet_ntoa(raw_address[20:24])
            except OSError:
                return None

    @staticmethod
    def get_interface_netmask(ifname: str) -> str | None:
        """Obtiene la mascara de subred IPv4 de una interfaz usando ioctl SIOCGIFNETMASK.

        Args:
            ifname: Nombre de la interfaz (por ejemplo "eth0").

        Returns:
            La mascara de subred en formato texto, o None si no se pudo determinar.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                packed_ifname = struct.pack("256s", ifname.encode("utf-8")[:15])
                raw_netmask = fcntl.ioctl(sock.fileno(), SIOCGIFNETMASK, packed_ifname)
                return socket.inet_ntoa(raw_netmask[20:24])
            except OSError:
                return None

    @staticmethod
    def compute_broadcast_address(ip_address: str, netmask: str) -> str:
        """Calcula la direccion de broadcast de una subred IPv4.

        Args:
            ip_address: IP del equipo en esa subred.
            netmask: Mascara de subred asociada.

        Returns:
            La direccion de broadcast (por ejemplo "192.168.2.255" para 192.168.2.10/24).
        """
        network = ipaddress.IPv4Network(f"{ip_address}/{netmask}", strict=False)
        return str(network.broadcast_address)

    def find_active_ethernet(self) -> tuple[str, str, str] | None:
        """Busca la primera interfaz Ethernet activa con IPv4 asignada.

        Returns:
            Tupla (interfaz, ip_propia, ip_broadcast) o None si ninguna interfaz
            Ethernet activa tiene una IPv4 asignada.
        """
        for iface in self.list_ethernet_interfaces():
            ip_address = self.get_interface_ipv4(iface)
            netmask = self.get_interface_netmask(iface)
            if ip_address and netmask:
                broadcast_address = self.compute_broadcast_address(ip_address, netmask)
                return iface, ip_address, broadcast_address
        return None


# ---------------------------------------------------------------------------
# Pipeline de GStreamer
# ---------------------------------------------------------------------------


class GstStreamPipeline:
    """Construye y administra el pipeline de GStreamer que remuxea el H264
    nativo de la camara a paquetes RTP/UDP, usando bindings PyGObject
    (sin invocar gst-launch-1.0 como subproceso)."""

    def __init__(
        self,
        device_path: str,
        own_ip: str,
        broadcast_ip: str,
        on_playing: Callable[[], None] | None = None,
    ) -> None:
        """Inicializa el manejador de pipeline para un dispositivo y destino de red dados.

        Args:
            device_path: Nodo V4L2 de la camara, por ejemplo "/dev/video0".
            own_ip: IP propia del equipo, usada solo para el campo c= del SDP informativo.
            broadcast_ip: IP de broadcast del subnet, usada como destino real del udpsink.
            on_playing: Callback invocado una vez cuando el pipeline confirma (via el bus)
                que efectivamente alcanzo el estado PLAYING.
        """
        self.device_path = device_path
        self.own_ip = own_ip
        self.broadcast_ip = broadcast_ip
        self._on_playing = on_playing
        self.pipeline: Gst.Pipeline | None = None
        self._mainloop: GLib.MainLoop | None = None
        self._stop_reason: str | None = None
        self._reached_playing = False
        self._playing_deadline: float | None = None

    def _make_element(self, factory_name: str, element_name: str) -> Gst.Element:
        """Crea un elemento de GStreamer o lanza un error claro si el plugin no esta instalado."""
        element = Gst.ElementFactory.make(factory_name, element_name)
        if element is None:
            raise RuntimeError(
                f"No se pudo crear el elemento GStreamer '{factory_name}'. Verifique que los "
                "plugins de GStreamer esten instalados (gstreamer1.0-plugins-base/good/bad)."
            )
        return element

    def _configure_broadcast_socket(self, sink: Gst.Element) -> None:
        """Habilita el envio a la IP de broadcast en el udpsink, de forma compatible
        con cualquier version de GStreamer.

        La propiedad "broadcast" de GstUDPSink solo existe desde GStreamer 1.20;
        sin SO_BROADCAST en el socket, el kernel rechaza con EACCES cualquier
        sendto() hacia una IP de broadcast. Por eso se crea el socket UDP a mano
        con SO_BROADCAST y se entrega al elemento via su propiedad "socket"
        (disponible desde GStreamer 1.0), en vez de depender de "broadcast".
        """
        raw_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        gio_socket = Gio.Socket.new_from_fd(raw_socket.detach())
        sink.set_property("socket", gio_socket)
        sink.set_property("close-socket", True)

    def build(self) -> None:
        """Construye el pipeline: v4l2src -> queue -> capsfilter(H264) -> h264parse ->
        rtph264pay -> udpsink, sin recodificar el video en ningun punto.

        El config-interval de h264parse/rtph264pay replica el usado por
        mavlink-camera-manager (BlueOS) para camaras UVC con H264 nativo, que es el
        mismo estandar de referencia que este script sigue para ser compatible con
        QGroundControl/VLC.

        Nota: se evita deliberadamente forzar stream-format=avc despues de h264parse
        (como hace BlueOS) porque esa renegociacion de caps puede quedar esperando
        indefinidamente -sin emitir ningun ERROR en el bus- si la camara no la
        soporta, dejando el pipeline "congelado" en PAUSED. Es una optimizacion de
        CPU, no un requisito funcional, así que no vale el riesgo de un cuelgue
        silencioso.

        Raises:
            RuntimeError: Si algun elemento de GStreamer requerido no puede crearse.
        """
        self.pipeline = Gst.Pipeline.new("usb_h264_streamer_pipeline")

        source = self._make_element("v4l2src", "camera_source")
        source.set_property("device", self.device_path)
        source.set_property("do-timestamp", True)

        # Queue para desacoplar la captura de la escritura en red. Sin "leaky":
        # al ser H264 nativo sin recodificar, descartar un solo frame P rompe la
        # cadena de referencias hasta el proximo keyframe, causando parpadeo/ruido
        # visible que se autocorrige recien en el siguiente GOP. Se usan los
        # limites por defecto de GStreamer (sin perdida, ~1s de margen), suficientes
        # para absorber micro-hiccups sin descartar buffers.
        queue = self._make_element("queue", "capture_queue")

        caps_filter = self._make_element("capsfilter", "camera_caps")
        caps_string = (
            f"video/x-h264,width={STREAM_WIDTH},height={STREAM_HEIGHT},"
            f"framerate={STREAM_FRAMERATE}/1"
        )
        caps_filter.set_property("caps", Gst.Caps.from_string(caps_string))

        # config-interval=-1 reenvia SPS/PPS solo junto a cada keyframe real (IDR),
        # nunca a mitad de GOP. El valor anterior (1 = temporizador fijo de 1s) forzaba
        # la reinsercion de SPS/PPS en un instante arbitrario del GOP, lo que el
        # decodificador interpreta como un cambio de parametros y produce el
        # parpadeo/retroceso periodico reportado.
        parser = self._make_element("h264parse", "h264_parser")
        parser.set_property("config-interval", -1)

        payloader = self._make_element("rtph264pay", "rtp_payloader")
        payloader.set_property("pt", RTP_PAYLOAD_TYPE)
        payloader.set_property("config-interval", -1)
        try:
            # Reduce el retardo de empaquetado (no disponible en versiones viejas
            # de rtph264pay); si falta, se sigue funcionando sin esta optimizacion.
            payloader.set_property("aggregate-mode", "zero-latency")
        except TypeError:
            logger.debug("rtph264pay no soporta 'aggregate-mode' en esta version de GStreamer.")

        sink = self._make_element("udpsink", "udp_sink")
        sink.set_property("host", self.broadcast_ip)
        sink.set_property("port", STREAM_UDP_PORT)
        sink.set_property("sync", False)
        sink.set_property("async", False)
        self._configure_broadcast_socket(sink)

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
        """Intercepta las caps negociadas por rtph264pay para extraer dinamicamente
        sprop-parameter-sets y profile-level-id (SPS/PPS reales del stream), y
        generar con ellos el descriptor SDP de referencia. Se auto-remueve tras el
        primer exito."""
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
        """Genera y registra el descriptor SDP de referencia a partir de datos reales del stream.

        Args:
            sprop_parameter_sets: SPS/PPS codificados en base64, extraidos del stream real.
            profile_level_id: Identificador de perfil/nivel H264, extraido del stream real.
        """
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
        """Maneja mensajes del bus de GStreamer; ERROR/EOS detienen el mainloop para reconectar."""
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
                    self._on_playing = None  # notificar una sola vez por corrida
        return True

    def _check_device_present(self) -> bool:
        """Callback periodico (GLib) que detecta la desconexion fisica de la camara y,
        de paso, vigila que el pipeline confirme PLAYING dentro de PLAYING_TIMEOUT_SECONDS.

        Siempre devuelve True (para que GLib lo siga llamando); la remocion de este
        callback se hace de forma explicita y unica en el bloque finally de run(),
        evitando una doble remocion (auto-remocion aqui + remocion en run()) que
        generaria una advertencia espuria de GLib al reintentar remover un source ya
        eliminado.

        Returns:
            Siempre True.
        """
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
        """Pone el pipeline en PLAYING y bloquea ejecutando el mainloop hasta que
        ocurra un error, EOS, perdida de camara o se solicite apagado externo.

        Returns:
            Motivo de detencion: "error", "eos", "device_lost", "shutdown_requested"
            o None si el mainloop nunca llego a iniciarse.
        """
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
            "Streaming solicitado: dispositivo=%s destino=udp://%s:%d (broadcast, pt=%d)",
            self.device_path, self.broadcast_ip, STREAM_UDP_PORT, RTP_PAYLOAD_TYPE,
        )

        try:
            self._mainloop.run()
        finally:
            GLib.source_remove(device_check_id)
            bus.disconnect(bus_handler_id)
            bus.remove_signal_watch()

        return self._stop_reason

    def quit(self) -> None:
        """Solicita la detencion externa del mainloop (usado por el manejador de SIGINT/SIGTERM)."""
        self._stop_reason = "shutdown_requested"
        if self._mainloop and self._mainloop.is_running():
            self._mainloop.quit()

    def stop(self) -> None:
        """Libera el pipeline de GStreamer de forma segura (estado NULL)."""
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None


# ---------------------------------------------------------------------------
# Aplicacion principal
# ---------------------------------------------------------------------------


class StreamerApplication:
    """Orquesta el ciclo completo: deteccion de camara, deteccion de red,
    construccion/ejecucion del pipeline y reconexion automatica ante fallas."""

    def __init__(self) -> None:
        """Inicializa la aplicacion y registra los manejadores de senal SIGINT/SIGTERM."""
        self._shutdown_requested = False
        self._current_pipeline: GstStreamPipeline | None = None
        signal.signal(signal.SIGINT, self._handle_shutdown_signal)
        signal.signal(signal.SIGTERM, self._handle_shutdown_signal)

    def _handle_shutdown_signal(self, signum: int, frame: FrameType | None) -> None:
        """Maneja SIGINT/SIGTERM: solicita cierre limpio del pipeline activo y del bucle principal."""
        logger.info("Senal de interrupcion recibida (signum=%d), cerrando de forma limpia...", signum)
        self._shutdown_requested = True
        if self._current_pipeline is not None:
            self._current_pipeline.quit()

    def _wait_for_camera(self) -> str | None:
        """Reintenta la deteccion de camara USB compatible hasta encontrarla o hasta el cierre solicitado."""
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

    def _wait_for_network(self) -> tuple[str, str, str] | None:
        """Reintenta la deteccion de interfaz Ethernet activa hasta encontrarla o hasta el cierre solicitado."""
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
        """Bucle principal: detecta camara y red, transmite, y se reconecta automaticamente ante fallas."""
        logger.info("Iniciando %s %s", APP_NAME, APP_VERSION)

        while not self._shutdown_requested:
            device_path = self._wait_for_camera()
            if device_path is None:
                break
            logger.info("Camara detectada: %s", device_path)

            network_info = self._wait_for_network()
            if network_info is None:
                break
            iface, own_ip, broadcast_ip = network_info
            logger.info(
                "Interfaz Ethernet activa: %s (ip=%s, broadcast=%s)", iface, own_ip, broadcast_ip
            )

            def _on_playing(iface: str = iface, own_ip: str = own_ip) -> None:
                print_video_target(iface, own_ip, STREAM_UDP_PORT)
                print_status(STATUS_RUNNING)

            gst_pipeline = GstStreamPipeline(
                device_path, own_ip, broadcast_ip, on_playing=_on_playing
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
    """Verifica que el binario v4l2-ctl (paquete v4l-utils) este disponible antes de iniciar.

    Termina el proceso con un mensaje claro si falta, en vez de fallar de forma
    confusa durante la deteccion de camara.
    """
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
    configure_logging()
    check_required_system_tools()
    Gst.init(None)
    app = StreamerApplication()
    app.run()


if __name__ == "__main__":
    main()
