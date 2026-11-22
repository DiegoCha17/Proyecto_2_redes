"""Pruebas del flujo CU-02: política, aprobación e idempotencia."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import pytest

from agente.catalogo import GESTIONAR_BLOQUEO, Catalogo, Herramienta, catalogo_por_defecto
from agente.cerebro import Cerebro, RegistroSesion
from agente.ejecutar import aprobar, ejecutar
from agente.interpretar import Intencion, TIPO_HERRAMIENTA
from agente.politicas import preparar
from config.ajustes import cargar_ajustes
from herramientas.comun import ErrorHerramienta
from tests.test_comun import ENTORNO_PRUEBA


ARGUMENTOS = {"equipo": "r1", "ip": "192.168.10.60", "accion": "bloquear"}


def ajustes():
    with patch.dict(os.environ, ENTORNO_PRUEBA, clear=True):
        return cargar_ajustes()


class FuncionGestion:
    def __init__(self, *, changed=True):
        self.llamadas = []
        self.changed = changed

    def __call__(self, **kwargs):
        self.llamadas.append(kwargs)
        return {
            "ok": True,
            "equipo": "R1",
            "ip": "192.168.10.60",
            "bloqueada": True,
            "changed": self.changed,
            "verified": True,
            "error": None,
        }


def catalogo_con(funcion):
    real = catalogo_por_defecto().obtener(GESTIONAR_BLOQUEO)
    return Catalogo([Herramienta(real.definicion, funcion)])


def test_sin_aprobacion_no_ejecuta():
    funcion = FuncionGestion()
    catalogo = catalogo_con(funcion)
    accion = preparar(ajustes(), GESTIONAR_BLOQUEO, ARGUMENTOS, catalogo=catalogo)

    resultado = ejecutar(accion, catalogo=catalogo, ajustes=ajustes())

    assert resultado.error["tipo"] == "aprobacion_requerida"
    assert resultado.ejecutada is False
    assert funcion.llamadas == []


def test_con_aprobacion_ejecuta_con_confirmacion_y_verifica():
    funcion = FuncionGestion()
    catalogo = catalogo_con(funcion)
    ajustes_actuales = ajustes()
    accion = preparar(ajustes_actuales, GESTIONAR_BLOQUEO, ARGUMENTOS, catalogo=catalogo)

    resultado = ejecutar(
        accion, aprobar(accion), catalogo=catalogo, ajustes=ajustes_actuales
    )

    assert resultado.ok is True
    assert resultado.aprobada is True
    assert resultado.salida["verified"] is True
    assert funcion.llamadas == [{**ARGUMENTOS, "confirmacion": True, "ajustes": ajustes_actuales}]


def test_bloquear_idempotente_no_reporta_cambio_si_ya_existe():
    funcion = FuncionGestion(changed=False)
    catalogo = catalogo_con(funcion)
    ajustes_actuales = ajustes()
    accion = preparar(ajustes_actuales, GESTIONAR_BLOQUEO, ARGUMENTOS, catalogo=catalogo)

    resultado = ejecutar(
        accion, aprobar(accion), catalogo=catalogo, ajustes=ajustes_actuales
    )

    assert resultado.ok is True
    assert resultado.salida["changed"] is False
    assert resultado.salida["verified"] is True


def test_ip_fuera_de_lan_rechazada_antes_de_aprobar():
    with pytest.raises(ErrorHerramienta) as error:
        preparar(
            ajustes(),
            GESTIONAR_BLOQUEO,
            {**ARGUMENTOS, "ip": "172.16.100.10"},
        )
    assert error.value.tipo == "ip_no_autorizada"


def test_cerebro_usa_vista_previa_aprobacion_y_verificacion():
    funcion = FuncionGestion()
    catalogo = catalogo_con(funcion)
    vistas = []
    preguntas = []
    with tempfile.TemporaryDirectory() as temporal:
        registro = RegistroSesion(directorio=temporal)
        cerebro = Cerebro(
            ajustes(),
            catalogo=catalogo,
            confirmar=lambda plan: preguntas.append(plan) or True,
            salida=vistas.append,
            registro=registro,
        )
        respuesta = cerebro._atender_herramienta(
            "Bloquea 192.168.10.60 en R1",
            Intencion(TIPO_HERRAMIENTA, GESTIONAR_BLOQUEO, ARGUMENTOS),
        )

    assert respuesta.ok is True
    assert len(preguntas) == 1
    assert len(funcion.llamadas) == 1
    assert any("192.168.10.60" in texto and "bloquear" in texto for texto in vistas)
    assert any("verificación posterior" in evento.get("nota", "") for evento in registro.eventos)


def test_cerebro_rechaza_y_no_ejecuta_si_operador_no_aprueba():
    funcion = FuncionGestion()
    catalogo = catalogo_con(funcion)
    with tempfile.TemporaryDirectory() as temporal:
        cerebro = Cerebro(
            ajustes(),
            catalogo=catalogo,
            confirmar=lambda plan: False,
            registro=RegistroSesion(directorio=temporal),
        )
        respuesta = cerebro._atender_herramienta(
            "Bloquea 192.168.10.60 en R1",
            Intencion(TIPO_HERRAMIENTA, GESTIONAR_BLOQUEO, ARGUMENTOS),
        )

    assert respuesta.tipo == "cancelado"
    assert funcion.llamadas == []

