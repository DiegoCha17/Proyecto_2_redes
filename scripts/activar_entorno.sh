#!/usr/bin/env bash
# =============================================================================
# Activa el entorno de desarrollo del Proyecto 2.
#
# Uso:
#   source scripts/activar_entorno.sh
#
# Hace dos cosas:
#   1. Exporta las variables de .env (necesarias para Ansible y NetAgent-IA).
#   2. Activa el entorno virtual .venv si existe.
# =============================================================================

PROYECTO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$PROYECTO/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    . "$PROYECTO/.env"
    set +a
    echo "Variables cargadas desde $PROYECTO/.env"
else
    echo "AVISO: no existe $PROYECTO/.env (copie .env.example a .env)" >&2
fi

if [[ -f "$PROYECTO/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    . "$PROYECTO/.venv/bin/activate"
    echo "Entorno virtual activado: $PROYECTO/.venv"
else
    echo "AVISO: no existe $PROYECTO/.venv; se usará el Python del sistema" >&2
fi
