#!/usr/bin/env python3
"""
Streaming en tiempo real de una camara USB UVC generica SIN H264 nativo (solo
MJPEG/YUYV, como la mayoria de camaras USB de bajo costo) hacia clientes unicast
via RTP/UDP, compatible con QGroundControl y VLC (mismo estandar que BlueOS /
mavlink-camera-manager). Funciona tanto en Linux (RPi/Ubuntu) como en Windows.

@author: Maquintel SpA
@date: 2026-08-04
@version: V1.0

Dependencias:
- GStreamer 1.0 + gst-plugins-base/good/bad/ugly (jpegdec, videoconvert, x264enc)
  y sus bindings de Python (PyGObject / gi).
    Ubuntu/RPi (paquetes verificados en Raspberry Pi OS/Debian 13):
        sudo apt install python3-gi gir1.2-gst-plugins-base-1.0 gstreamer1.0-tools \
            gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
            gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
    Windows:
        Instalar el runtime + development de GStreamer (gstreamer.freedesktop.org,
        build "MSVC 64-bit"), marcando TODOS los conjuntos de plugins durante la
        instalacion, y los bindings PyGObject para Windows (paquete MSYS2
        mingw-w64-x86_64-python-gobject, o wheels equivalentes).
- psutil (unica dependencia externa de Python): pip install psutil

Diferencia principal respecto a cam_usb_h264_streamer_unicast_RPi_ubunt.py:
- Ese script asume que la camara entrega H264 nativo y arma un pipeline de solo
  remuxeo (v4l2src -> h264parse -> rtph264pay), sin recodificar.
- La camara USB caracterizada para este script (verificado con
  `ffmpeg -f dshow -list_options -i video="<nombre>"` en Windows, equivalente a
  `v4l2-ctl --list-formats-ext` en Linux) NO entrega H264 en ningun modo: solo
  MJPEG (hasta 1280x720@30fps y superior) y YUYV crudo. Por lo tanto este script
  decodifica MJPEG y recodifica a H264 antes de empaquetar RTP: usa el encoder de
  hardware V4L2 M2M (v4l2h264enc) cuando esta disponible (Raspberry Pi 3/4 con
  bcm2835-codec), y cae a x264enc por software en cualquier otro equipo (Windows,
  RPi5 -que no trae encoder H264 dedicado-, u otros Linux sin V4L2 M2M para H264).
  Esto es distinto del passthrough del script original (que no recodifica nada)
  pero es la unica forma de llegar a H264 con este modelo de camara.

Decisiones tecnicas relevantes:
- Deteccion de camara multiplataforma via Gst.DeviceMonitor (API nativa de
  GStreamer), en vez de v4l2-ctl (solo Linux) o de parsear la salida de
  `ffmpeg -f dshow` (solo Windows, y agregaria una dependencia extra a ffmpeg).
  Ademas, Gst.Device.create_element() crea automaticamente el elemento fuente
  correcto para cada plataforma (v4l2src en Linux, ksvideosrc/mfvideosrc en
  Windows) ya apuntando al dispositivo fisico elegido, sin bifurcar el codigo
  por sistema operativo.
- Deteccion de red via psutil en vez de ioctl/sysfs (solo Linux en el script
  original): unica forma de obtener IP+netmask de forma identica en Linux y
  Windows sin reimplementar la logica dos veces. En este script la IP propia
  detectada es solo informativa (va en el campo c= del SDP de referencia); el
  destino real del stream son las IPs de cliente pasadas por argumento via
  multiudpsink. Por eso no se exige que la interfaz sea especificamente Ethernet
  (a diferencia del script original): alcanza con la primera interfaz activa
  no-loopback/no-virtual con IPv4, sea Ethernet o WiFi. Si no se encuentra
  ninguna, se usa "0.0.0.0" en el SDP y el streaming continua igual (no bloquea).
- La perdida fisica de la camara se detecta re-enumerando periodicamente los
  dispositivos de video (Gst.DeviceMonitor) y verificando que el nombre del
  dispositivo elegido siga apareciendo, en vez de sondear una ruta de archivo
  tipo /dev/videoX (que no existe como concepto en Windows).
- No se usa asyncio: igual que en el script original, GLib.MainLoop cubre por
  completo el bucle de eventos de GStreamer (captura -> pipeline -> reintento).
- Destino UDP = unicast a los clientes indicados por argumento (multiudpsink),
  igual que en cam_usb_h264_streamer_unicast_RPi_ubunt.py.

Uso:
    python cam_usb_MJPEG-YUY2_streamer_unicast.py <ip_cliente_1> [ip_cliente_2 ...]
"""

from __future__ import annotations

import logging
import signal
import socket
import sys
import time
import uuid
from types import FrameType
from typing import Callable

try:
    import psutil
except ImportError as import_error:
    sys.stderr.write(
        "Error: falta la libreria 'psutil'. Instalela con: pip install psutil\n"
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
        "Vea las instrucciones de instalacion en el encabezado de este script.\n"
        f"Detalle: {import_error}\n"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constantes de configuracion
# ---------------------------------------------------------------------------

APP_NAME = "usb_h264_streamer_multiplatform"
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

ENCODER_BITRATE_KBPS = 4000
"""Bitrate objetivo del encoder H264 (kbps), balance calidad/CPU para 720p30."""

ENCODER_KEY_INT_MAX = STREAM_FRAMERATE * 2
"""Intervalo maximo entre keyframes (~2s a 30fps), igual de criterio que config-interval=-1."""

HARDWARE_ENCODER_NAME = "v4l2h264enc"
"""Encoder H264 por hardware (V4L2 M2M) disponible en Raspberry Pi 3/4 (bcm2835-codec).
No existe en Raspberry Pi 5 (sin encoder H264 dedicado) ni en Windows."""

SOFTWARE_ENCODER_NAME = "x264enc"
"""Encoder H264 por software, usado como fallback cuando no hay encoder de hardware
disponible (Windows, RPi5, u otros equipos sin V4L2 M2M para H264)."""

RECONNECT_DELAY_SECONDS = 4
"""Tiempo de espera entre reintentos de deteccion/streaming tras una falla."""

DEVICE_POLL_INTERVAL_SECONDS = 3
"""Intervalo de verificacion de presencia fisica de la camara durante el streaming
(re-enumeracion via Gst.DeviceMonitor; menos frecuente que en el script original
porque cada verificacion abre/cierra un monitor de dispositivos)."""

PLAYING_TIMEOUT_SECONDS = 10
"""Tiempo maximo de espera a que el pipeline confirme PLAYING antes de abortar y reintentar.

Cubre el caso de una negociacion de caps que se cuelga indefinidamente sin emitir
ningun mensaje de ERROR en el bus (p. ej. un modo que la camara no soporta en la
practica pese a anunciarlo): sin este watchdog, el mainloop se queda esperando
para siempre sin dar ninguna senal."""

SDP_OUTPUT_PATH = "stream_reference.sdp"
"""Ruta donde se escribe el descriptor SDP informativo, generado dinamicamente desde el stream real."""

EXCLUDED_INTERFACE_PATTERNS = (
    "lo", "loopback", "docker", "veth", "br-", "virbr", "tun", "tap", "vethernet", "bluetooth",
)
"""Subcadenas (en minuscula) de nombres de interfaz virtuales/loopback a excluir al
buscar la interfaz de red activa, validas tanto para nombres de Linux (lo, docker0,
veth...) como de Windows (vEthernet (Default Switch), Bluetooth Network Connection)."""

LOG_FILE_PATH = "usb_h264_streamer_multiplatform.log"
"""Archivo donde se registra el detalle tecnico (misma logica que BlueOS/mavlink-camera-manager,
que dejan el detalle en el log del servicio y muestran solo el estado al operador)."""

STATUS_RUNNING = "running!"
"""Texto de estado mostrado en consola mientras el streaming esta activo."""

STATUS_CANCELLED = "cancelado"
"""Texto de estado mostrado en consola al cerrar la aplicacion (Ctrl+C/SIGTERM)."""

REQUIRED_GST_ELEMENTS = (
    "queue", "capsfilter", "jpegdec", "videoconvert",
    "h264parse", "rtph264pay", "multiudpsink",
)
"""Elementos de GStreamer usados por el pipeline, independientes de la fuente de video
(la fuente la crea Gst.Device.create_element() con el plugin correcto por plataforma) y
del encoder H264 (hardware o software, verificado aparte por check_h264_encoder_available())."""

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
# Deteccion de camara USB (multiplataforma, via Gst.DeviceMonitor)
# ---------------------------------------------------------------------------


class CameraDetector:
    """Detecta automaticamente la primera camara de video que entrega MJPEG en la
    resolucion y framerate configurados, usando el monitor de dispositivos nativo
    de GStreamer (misma API en Linux y Windows)."""

    @staticmethod
    def _iter_video_source_devices() -> list["Gst.Device"]:
        monitor = Gst.DeviceMonitor.new()
        monitor.add_filter("Video/Source", None)
        if not monitor.start():
            logger.debug("No se pudo iniciar Gst.DeviceMonitor.")
            return []
        try:
            return list(monitor.get_devices())
        finally:
            monitor.stop()

    @staticmethod
    def _structure_matches_target_framerate(structure: "Gst.Structure") -> bool:
        try:
            ok, numerator, denominator = structure.get_fraction("framerate")
            if ok and denominator:
                return abs((numerator / denominator) - STREAM_FRAMERATE) < 0.5
        except (TypeError, ValueError):
            pass

        try:
            framerate_value = structure.get_value("framerate")
        except TypeError:
            return False
        if framerate_value is None:
            return False
        for candidate in framerate_value if isinstance(framerate_value, (list, tuple)) else ():
            numerator = getattr(candidate, "num", None)
            denominator = getattr(candidate, "denom", None)
            if numerator and denominator and abs((numerator / denominator) - STREAM_FRAMERATE) < 0.5:
                return True
        return False

    def _device_supports_target_format(self, device: "Gst.Device") -> bool:
        caps = device.get_caps()
        if caps is None:
            return False

        for index in range(caps.get_size()):
            structure = caps.get_structure(index)
            if structure.get_name() != "image/jpeg":
                continue

            width_ok, width = structure.get_int("width")
            height_ok, height = structure.get_int("height")
            if not (width_ok and height_ok) or (width, height) != (STREAM_WIDTH, STREAM_HEIGHT):
                continue

            if self._structure_matches_target_framerate(structure):
                return True

        return False

    def find_mjpeg_camera(self) -> "Gst.Device | None":
        for device in self._iter_video_source_devices():
            display_name = device.get_display_name()
            if self._device_supports_target_format(device):
                logger.debug(
                    "%s aceptada: MJPEG %dx%d@%dfps",
                    display_name, STREAM_WIDTH, STREAM_HEIGHT, STREAM_FRAMERATE,
                )
                return device
            logger.debug(
                "%s descartada: no soporta MJPEG %dx%d@%dfps",
                display_name, STREAM_WIDTH, STREAM_HEIGHT, STREAM_FRAMERATE,
            )
        return None

    def is_device_still_present(self, display_name: str) -> bool:
        return any(
            device.get_display_name() == display_name for device in self._iter_video_source_devices()
        )


# ---------------------------------------------------------------------------
# Deteccion de interfaz de red activa e IP (solo para el campo c= informativo del SDP)
# ---------------------------------------------------------------------------


class NetworkDetector:
    """Detecta la primera interfaz de red activa con IPv4 asignada, usando psutil
    para obtener el mismo resultado en Linux y Windows sin reimplementar la logica
    de enumeracion dos veces (ioctl/sysfs en un lado, API Win32 en el otro)."""

    @staticmethod
    def _is_excluded_interface(name: str) -> bool:
        lowered = name.lower()
        return any(pattern in lowered for pattern in EXCLUDED_INTERFACE_PATTERNS)

    def find_active_interface(self) -> tuple[str, str] | None:
        """Busca la primera interfaz activa (Ethernet o WiFi) con IPv4 asignada.

        Returns:
            Tupla (interfaz, ip_propia) o None si no se encontro ninguna. El
            resultado es solo informativo (campo c= del SDP): el destino real del
            stream son las IPs de cliente pasadas por argumento.
        """
        try:
            addrs_by_iface = psutil.net_if_addrs()
            stats_by_iface = psutil.net_if_stats()
        except OSError as error:
            logger.debug("No se pudo enumerar interfaces de red via psutil: %s", error)
            return None

        for iface, addrs in addrs_by_iface.items():
            if self._is_excluded_interface(iface):
                continue
            stats = stats_by_iface.get(iface)
            if stats is None or not stats.isup:
                continue
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    return iface, addr.address
        return None


# ---------------------------------------------------------------------------
# Pipeline de GStreamer
# ---------------------------------------------------------------------------


class GstStreamPipeline:
    """Construye y administra el pipeline de GStreamer que decodifica el MJPEG de
    la camara, lo recodifica a H264 por software y lo empaqueta como RTP/UDP
    unicast, usando bindings PyGObject (sin invocar gst-launch-1.0 como subproceso)."""

    def __init__(
        self,
        device: "Gst.Device",
        own_ip: str,
        client_ips: list[str],
        on_playing: Callable[[], None] | None = None,
        prefer_hardware_encoder: bool = True,
    ) -> None:
        """Inicializa el manejador de pipeline para una camara y destinos dados.

        Args:
            device: Dispositivo de video detectado por CameraDetector.
            own_ip: IP propia del equipo, usada solo para el campo c= del SDP informativo.
            client_ips: Lista de IPs unicast de los clientes a los que se envia el
                stream (equivalente al "clients=ip:puerto,..." de multiudpsink que
                usa mavlink-camera-manager/BlueOS).
            on_playing: Callback invocado una vez cuando el pipeline confirma (via el bus)
                que efectivamente alcanzo el estado PLAYING.
            prefer_hardware_encoder: Si es False, se fuerza el uso del encoder de
                software aunque exista uno de hardware disponible. Lo usa
                StreamerApplication para dejar de intentar el encoder de hardware
                despues de una falla en tiempo de ejecucion (ver run()).
        """
        self.device = device
        self.own_ip = own_ip
        self.client_ips = client_ips
        self._on_playing = on_playing
        self.prefer_hardware_encoder = prefer_hardware_encoder
        self.used_hardware_encoder = False
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

    def _make_h264_encoder(self) -> tuple[Gst.Element, bool]:
        """Crea el encoder H264, priorizando el encoder de hardware V4L2 M2M
        (v4l2h264enc) cuando esta disponible (Raspberry Pi 3/4 con bcm2835-codec),
        y usando x264enc por software como fallback en cualquier otro equipo
        (Windows, RPi5, PCs Linux sin V4L2 M2M para H264).

        Se prefiere hardware porque libera CPU para el resto de la aplicacion de
        inspeccion (procesamiento de sensores, telemetria, etc.), critico en un
        SoC ARM como el de la RPi.

        Returns:
            Tupla (elemento_encoder, es_hardware). El booleano lo usa build()
            para decidir si debe forzar el formato de entrada a NV12 (requerido
            por el driver bcm2835-codec; sin esto el encoder de hardware falla
            en tiempo de ejecucion con "Failed to process frame" pese a que el
            pipeline negocia caps sin error, porque v4l2h264enc anuncia soporte
            para varios formatos de entrada pero el M2M del SoC solo procesa
            NV12 de forma confiable).
        """
        if self.prefer_hardware_encoder and Gst.ElementFactory.find(HARDWARE_ENCODER_NAME) is not None:
            encoder = self._make_element(HARDWARE_ENCODER_NAME, "h264_encoder")
            # repeat_sequence_header=1 reinserta SPS/PPS en cada IDR (necesario para
            # que un cliente RTP que se une a mitad de stream, como VLC, pueda
            # decodificar sin esperar una reconexion); video_bitrate en bps (V4L2),
            # no en kbps como en x264enc.
            extra_controls = Gst.Structure.new_from_string(
                f"controls,repeat_sequence_header=(int)1,"
                f"video_bitrate=(int){ENCODER_BITRATE_KBPS * 1000}"
            )
            encoder.set_property("extra-controls", extra_controls)
            logger.info("Encoder H264 de hardware detectado: usando %s.", HARDWARE_ENCODER_NAME)
            return encoder, True

        encoder = self._make_element(SOFTWARE_ENCODER_NAME, "h264_encoder")
        encoder.set_property("tune", "zerolatency")
        encoder.set_property("speed-preset", "ultrafast")
        encoder.set_property("bitrate", ENCODER_BITRATE_KBPS)
        encoder.set_property("key-int-max", ENCODER_KEY_INT_MAX)
        logger.info(
            "No se detecto encoder de hardware (%s); usando encoder de software %s.",
            HARDWARE_ENCODER_NAME, SOFTWARE_ENCODER_NAME,
        )
        return encoder, False

    def build(self) -> None:
        """Construye el pipeline: <fuente especifica de plataforma> -> queue ->
        capsfilter(MJPEG) -> jpegdec -> videoconvert -> x264enc -> h264parse ->
        rtph264pay -> multiudpsink.

        A diferencia del script original (passthrough de H264 nativo), aqui la
        camara solo entrega MJPEG, por lo que se decodifica y se recodifica a
        H264 por software antes de empaquetar RTP.

        Raises:
            RuntimeError: Si algun elemento de GStreamer requerido no puede crearse.
        """
        self.pipeline = Gst.Pipeline.new("usb_h264_streamer_pipeline")

        # Gst.Device.create_element() ya devuelve el elemento fuente correcto y
        # configurado para esta plataforma (v4l2src con "device" en Linux,
        # ksvideosrc/mfvideosrc con el indice correspondiente en Windows).
        source = self.device.create_element("camera_source")

        queue = self._make_element("queue", "capture_queue")

        source_caps_filter = self._make_element("capsfilter", "camera_caps")
        source_caps_string = (
            f"image/jpeg,width={STREAM_WIDTH},height={STREAM_HEIGHT},"
            f"framerate={STREAM_FRAMERATE}/1"
        )
        source_caps_filter.set_property("caps", Gst.Caps.from_string(source_caps_string))

        jpeg_decoder = self._make_element("jpegdec", "jpeg_decoder")
        converter = self._make_element("videoconvert", "video_converter")

        encoder, using_hardware_encoder = self._make_h264_encoder()
        self.used_hardware_encoder = using_hardware_encoder

        # El encoder de hardware (v4l2h264enc/bcm2835-codec) solo procesa NV12 de
        # forma confiable pese a anunciar caps mas amplias; ver _make_h264_encoder().
        raw_caps_filter: Gst.Element | None = None
        if using_hardware_encoder:
            raw_caps_filter = self._make_element("capsfilter", "raw_video_caps")
            raw_caps_filter.set_property("caps", Gst.Caps.from_string("video/x-raw,format=NV12"))

        parser = self._make_element("h264parse", "h264_parser")
        parser.set_property("config-interval", -1)

        payloader = self._make_element("rtph264pay", "rtp_payloader")
        payloader.set_property("pt", RTP_PAYLOAD_TYPE)
        payloader.set_property("config-interval", -1)
        try:
            payloader.set_property("aggregate-mode", "zero-latency")
        except TypeError:
            logger.debug("rtph264pay no soporta 'aggregate-mode' en esta version de GStreamer.")

        # multiudpsink con "clients=ip:puerto,..." (unicast explicito a cada
        # cliente), igual que mavlink-camera-manager/BlueOS.
        clients = ",".join(f"{ip}:{STREAM_UDP_PORT}" for ip in self.client_ips)
        sink = self._make_element("multiudpsink", "udp_sink")
        sink.set_property("clients", clients)
        sink.set_property("sync", False)

        elements = [source, queue, source_caps_filter, jpeg_decoder, converter]
        if raw_caps_filter is not None:
            elements.append(raw_caps_filter)
        elements.extend((encoder, parser, payloader, sink))
        for element in elements:
            self.pipeline.add(element)

        source.link(queue)
        queue.link(source_caps_filter)
        source_caps_filter.link(jpeg_decoder)
        jpeg_decoder.link(converter)
        if raw_caps_filter is not None:
            converter.link(raw_caps_filter)
            raw_caps_filter.link(encoder)
        else:
            converter.link(encoder)
        encoder.link(parser)
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

    def _check_camera_and_timeout(self) -> bool:
        if not CameraDetector().is_device_still_present(self.device.get_display_name()):
            logger.error("La camara '%s' ya no esta presente.", self.device.get_display_name())
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
            DEVICE_POLL_INTERVAL_SECONDS, self._check_camera_and_timeout
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
            "Streaming solicitado: camara=%s destino=%s (unicast, pt=%d)",
            self.device.get_display_name(),
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
        self._prefer_hardware_encoder = True
        signal.signal(signal.SIGINT, self._handle_shutdown_signal)
        signal.signal(signal.SIGTERM, self._handle_shutdown_signal)

    def _handle_shutdown_signal(self, signum: int, frame: FrameType | None) -> None:
        logger.info("Senal de interrupcion recibida (signum=%d), cerrando de forma limpia...", signum)
        self._shutdown_requested = True
        if self._current_pipeline is not None:
            self._current_pipeline.quit()

    def _wait_for_camera(self) -> "Gst.Device | None":
        camera_detector = CameraDetector()
        while not self._shutdown_requested:
            device = camera_detector.find_mjpeg_camera()
            if device:
                return device
            logger.warning(
                "No se detecto ninguna camara compatible con MJPEG %dx%d@%dfps. "
                "Reintentando en %d s...",
                STREAM_WIDTH, STREAM_HEIGHT, STREAM_FRAMERATE, RECONNECT_DELAY_SECONDS,
            )
            time.sleep(RECONNECT_DELAY_SECONDS)
        return None

    def _detect_own_ip(self) -> str:
        """Detecta la IP propia solo con fines informativos (campo c= del SDP).

        A diferencia de la deteccion de camara, esto no bloquea el streaming: si
        no se encuentra ninguna interfaz activa se usa "0.0.0.0" y se continua,
        porque el destino real del stream ya viene fijado por los argumentos de
        linea de comando.
        """
        network_info = NetworkDetector().find_active_interface()
        if network_info is None:
            logger.warning(
                "No se detecto ninguna interfaz de red activa con IPv4; "
                "se usara 0.0.0.0 en el SDP informativo."
            )
            return "0.0.0.0"
        iface, ip_address = network_info
        logger.info("Interfaz de red activa detectada: %s (ip=%s)", iface, ip_address)
        return ip_address

    def run(self) -> None:
        logger.info(
            "Iniciando %s %s (clientes unicast: %s)", APP_NAME, APP_VERSION, ", ".join(self.client_ips)
        )

        while not self._shutdown_requested:
            device = self._wait_for_camera()
            if device is None:
                break
            logger.info("Camara detectada: %s", device.get_display_name())

            own_ip = self._detect_own_ip()

            def _on_playing(client_ips: list[str] = self.client_ips) -> None:
                print_video_target(client_ips, STREAM_UDP_PORT)
                print_status(STATUS_RUNNING)

            gst_pipeline = GstStreamPipeline(
                device, own_ip, self.client_ips, on_playing=_on_playing,
                prefer_hardware_encoder=self._prefer_hardware_encoder,
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
                used_hardware_encoder = gst_pipeline.used_hardware_encoder
                gst_pipeline.stop()
                self._current_pipeline = None

            if self._shutdown_requested:
                break

            if stop_reason == "error" and used_hardware_encoder and self._prefer_hardware_encoder:
                logger.warning(
                    "El encoder de hardware %s fallo en tiempo de ejecucion "
                    "('Failed to process frame'). Esto suele deberse a memoria "
                    "CMA/GPU insuficiente reservada para bcm2835-codec en "
                    "/boot/firmware/config.txt (revisar 'dtoverlay=vc4-kms-v3d,cma-<N>' "
                    "o 'gpu_mem'). Se usara el encoder de software %s en los "
                    "proximos intentos de esta ejecucion.",
                    HARDWARE_ENCODER_NAME, SOFTWARE_ENCODER_NAME,
                )
                self._prefer_hardware_encoder = False

            logger.warning(
                "Streaming detenido (motivo=%s). Reintentando en %d s...",
                stop_reason, RECONNECT_DELAY_SECONDS,
            )
            time.sleep(RECONNECT_DELAY_SECONDS)

        logger.info("%s finalizado.", APP_NAME)
        print_status(STATUS_CANCELLED)


def check_required_gst_elements() -> None:
    """Verifica que los plugins de GStreamer requeridos por el pipeline esten
    disponibles (independiente de la fuente de video, que se resuelve por
    plataforma via Gst.Device.create_element()).

    Termina el proceso con instrucciones claras de instalacion si falta alguno,
    en vez de fallar de forma confusa durante la construccion del pipeline.
    """
    missing = [name for name in REQUIRED_GST_ELEMENTS if Gst.ElementFactory.find(name) is None]
    if missing:
        message = (
            "Faltan plugins de GStreamer requeridos: " + ", ".join(missing) + "\n"
            "Ubuntu/RPi: sudo apt install gstreamer1.0-plugins-base gstreamer1.0-plugins-good "
            "gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly\n"
            "Windows: reinstale GStreamer marcando TODOS los conjuntos de plugins "
            "(base/good/bad/ugly) en el instalador de gstreamer.freedesktop.org.\n"
        )
        logger.error(message)
        sys.stderr.write(f"Error fatal: {message}")
        sys.exit(1)


def check_h264_encoder_available() -> None:
    """Verifica que exista al menos un encoder H264 (hardware o software).

    Termina el proceso con instrucciones claras si no se encuentra ninguno, en
    vez de fallar de forma confusa durante la construccion del pipeline.
    """
    if Gst.ElementFactory.find(HARDWARE_ENCODER_NAME) is not None:
        return
    if Gst.ElementFactory.find(SOFTWARE_ENCODER_NAME) is not None:
        return
    message = (
        f"No se encontro ningun encoder H264 ({HARDWARE_ENCODER_NAME} ni "
        f"{SOFTWARE_ENCODER_NAME}).\n"
        "Ubuntu/RPi: sudo apt install gstreamer1.0-plugins-ugly gstreamer1.0-plugins-good\n"
        "Windows: reinstale GStreamer marcando el conjunto de plugins 'ugly'.\n"
    )
    logger.error(message)
    sys.stderr.write(f"Error fatal: {message}")
    sys.exit(1)


def main() -> None:
    """Punto de entrada: configura logging, inicializa GStreamer, valida
    dependencias y arranca la aplicacion."""
    if len(sys.argv) < 2:
        sys.stderr.write(
            "Uso: cam_usb_MJPEG-YUY2_streamer_unicast.py <ip_cliente_1> [ip_cliente_2 ...]\n"
            "Ejemplo: cam_usb_MJPEG-YUY2_streamer_unicast.py 192.168.1.200\n"
        )
        sys.exit(1)
    client_ips = sys.argv[1:]

    configure_logging()
    Gst.init(None)
    check_required_gst_elements()
    check_h264_encoder_available()
    app = StreamerApplication(client_ips)
    app.run()


if __name__ == "__main__":
    main()
