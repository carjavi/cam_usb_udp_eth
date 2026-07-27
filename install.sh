#!/usr/bin/env bash
# cam_usb_h264_streamer - instalador unico
# @author: Carlos Briceno <carjavi@hotmail.com>
# @version: V1.0
#
# Instala TODO lo necesario para correr cam_usb_h264_streamer.py: paquetes de
# sistema (GStreamer, v4l-utils, headers de compilacion) y PyGObject dentro
# de un venv, compilado especificamente para el interprete de ese venv (evita
# el error "cannot import name '_gi' from partially initialized module 'gi'"
# que ocurre al depender del PyGObject del sistema con --system-site-packages).
#
# Uso:
#   chmod +x install.sh
#   ./install.sh

set -euo pipefail

VENV_DIR="venv"
PYGOBJECT_SPEC="PyGObject>=3.42,<4"

echo "== 1/3: paquetes de sistema =="

# "|| true": en algunas instalaciones de Ubuntu, el hook de post-actualizacion
# de "command-not-found" (cnf-update-db) falla por un problema propio del
# sistema (python3-apt) sin relacion con este proyecto; apt-get update ya
# actualizo la lista de paquetes correctamente antes de llegar a ese hook, asi
# que no debe abortar el resto de la instalacion.
sudo apt-get update || true

# Se instalan en grupos separados a proposito: "apt-get install" es atomico
# -si UN SOLO nombre de paquete no existe en esta version de Ubuntu, no
# instala NINGUNO de la lista, ni siquiera los que si existen. Separando por
# grupo, un nombre de paquete GStreamer que cambie entre versiones de Ubuntu
# no bloquea la instalacion de las herramientas de compilacion que pip
# necesita para PyGObject.

sudo apt-get install -y \
    pkg-config \
    python3-dev \
    python3-venv \
    libcairo2-dev

sudo apt-get install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gir1.2-gst-plugins-base-1.0 \
    v4l-utils

# El paquete de cabeceras de GObject-Introspection requerido para compilar
# PyGObject via pip cambio de nombre entre versiones de Ubuntu
# (girepository-1.0 -> girepository-2.0, desde Ubuntu 24.04+). Se intenta el
# nombre nuevo primero y se cae al anterior si no existe en esta version.
sudo apt-get install -y libgirepository-2.0-dev \
    || sudo apt-get install -y libgirepository1.0-dev

echo ""
echo "== 2/3: entorno virtual (venv) =="

if [ -f "$VENV_DIR/pyvenv.cfg" ] && grep -q "include-system-site-packages = true" "$VENV_DIR/pyvenv.cfg"; then
    echo "El venv existente en '$VENV_DIR' tiene --system-site-packages activo."
    echo "Con esa bandera, pip ve el PyGObject del sistema como 'ya satisfecho'"
    echo "y no instala la copia compilada para este venv (causa del error _gi)."
    echo "Se recreara sin esa bandera."
    rm -rf "$VENV_DIR"
fi

if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi

echo ""
echo "== 3/3: PyGObject (compilado especificamente para este venv) =="

"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install "$PYGOBJECT_SPEC"

echo ""
echo "Listo. Para correr la app:"
echo "  source $VENV_DIR/bin/activate"
echo "  python cam_usb_h264_streamer.py"
