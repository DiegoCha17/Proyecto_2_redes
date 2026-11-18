"""Pruebas de herramientas/comun.py sin conexiones SSH reales."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from netmiko import NetmikoAuthenticationException, NetmikoTimeoutException, ReadTimeout
from netmiko.ssh_dispatcher import CLASS_MAPPER

from config.ajustes import ErrorConfiguracion, cargar_ajustes
from herramientas.comun import (
    COMANDO_DIRECCIONES,
    COMANDO_INTERFACES,
    COMANDOS_LECTURA,
    TIMEOUT_CONEXION,
    TIMEOUT_LECTURA,
    ErrorHerramienta,
    abrir_conexion,
    cerrar_conexion,
    conexion_dispositivo,
    construir_respuesta,
    ejecutar_lectura,
    error_json,
)

ENTORNO_PRUEBA = {
    "NETAGENT_R1_USUARIO": "usuario_prueba",
    "NETAGENT_R1_CLAVE": "clave-prueba-r1",
    "NETAGENT_SW1_USUARIO": "usuario_prueba",
    "NETAGENT_SW1_CLAVE": "clave-prueba-sw1",
}


class ConexionFalsa:
    """Doble de prueba de una sesión Netmiko."""

    def __init__(self, respuestas=None, error=None, fallo_al_cerrar=False):
        self.respuestas = respuestas or {}
        self.error = error
        self.fallo_al_cerrar = fallo_al_cerrar
        self.comandos: list[str] = []
        self.ultimos_kwargs: dict = {}
        self.desconectada = False

    def send_command(self, comando, **kwargs):
        self.comandos.append(comando)
        self.ultimos_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.respuestas.get(comando, "")

    def disconnect(self):
        self.desconectada = True
        if self.fallo_al_cerrar:
            raise OSError("fallo simulado al cerrar")


class PruebaSobreJSON(unittest.TestCase):
    def test_sobre_correcto_tiene_estructura_esperada(self):
        respuesta = construir_respuesta("R1")
        self.assertEqual(
            list(respuesta), ["ok", "equipo", "interfaces", "changed", "error"]
        )
        self.assertTrue(respuesta["ok"])
        self.assertEqual(respuesta["equipo"], "R1")
        self.assertEqual(respuesta["interfaces"], [])
        self.assertFalse(respuesta["changed"])
        self.assertIsNone(respuesta["error"])

    def test_sobre_con_error_no_es_ok(self):
        respuesta = construir_respuesta(
            "R1", error=error_json("conexion", "sin ruta al equipo")
        )
        self.assertFalse(respuesta["ok"])
        self.assertEqual(respuesta["error"]["tipo"], "conexion")
        json.dumps(respuesta, ensure_ascii=False)  # serializable


class PruebaConexion(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()

    def test_driver_mikrotik_registrado_en_netmiko(self):
        self.assertEqual(
            CLASS_MAPPER["mikrotik_routeros"].__name__, "MikrotikRouterOsSSH"
        )

    def test_abrir_conexion_usa_configuracion_y_entorno(self):
        falsa = ConexionFalsa()
        with patch("herramientas.comun.ConnectHandler", return_value=falsa) as mock:
            conexion = abrir_conexion("r1", ajustes=self.ajustes)

        self.assertIs(conexion, falsa)
        parametros = mock.call_args.kwargs
        self.assertEqual(parametros["device_type"], "mikrotik_routeros")
        self.assertEqual(parametros["host"], "10.60.30.202")
        self.assertEqual(parametros["port"], 22)
        self.assertEqual(parametros["username"], ENTORNO_PRUEBA["NETAGENT_R1_USUARIO"])
        self.assertEqual(parametros["password"], ENTORNO_PRUEBA["NETAGENT_R1_CLAVE"])
        self.assertEqual(parametros["conn_timeout"], TIMEOUT_CONEXION)
        self.assertFalse(parametros["fast_cli"])

    def test_comando_fuera_del_catalogo_no_se_ejecuta(self):
        falsa = ConexionFalsa()
        with self.assertRaises(ErrorHerramienta) as contexto:
            ejecutar_lectura(falsa, "/system reboot")
        self.assertEqual(contexto.exception.tipo, "comando_no_autorizado")
        self.assertEqual(falsa.comandos, [])

    def test_catalogo_solo_contiene_comandos_de_lectura(self):
        self.assertEqual(
            COMANDOS_LECTURA, frozenset({COMANDO_INTERFACES, COMANDO_DIRECCIONES})
        )
        for comando in COMANDOS_LECTURA:
            self.assertIn("print", comando)
            self.assertNotIn("set", comando)

    def test_lectura_autorizada_pasa_timeout_y_devuelve_salida(self):
        falsa = ConexionFalsa({COMANDO_INTERFACES: "salida routeros"})
        salida = ejecutar_lectura(falsa, COMANDO_INTERFACES)
        self.assertEqual(salida, "salida routeros")
        self.assertEqual(falsa.ultimos_kwargs["read_timeout"], TIMEOUT_LECTURA)
        self.assertTrue(falsa.ultimos_kwargs["strip_prompt"])
        self.assertTrue(falsa.ultimos_kwargs["strip_command"])

    def test_lectura_con_timeout_informa_tiempo_agotado(self):
        for error in (ReadTimeout("sin datos"), NetmikoTimeoutException("sin datos")):
            falsa = ConexionFalsa(error=error)
            with self.assertRaises(ErrorHerramienta) as contexto:
                ejecutar_lectura(falsa, COMANDO_DIRECCIONES)
            self.assertEqual(contexto.exception.tipo, "tiempo_agotado")

    def test_error_de_autenticacion_no_expone_clave(self):
        with patch(
            "herramientas.comun.ConnectHandler",
            side_effect=NetmikoAuthenticationException("credenciales inválidas"),
        ):
            with self.assertRaises(ErrorHerramienta) as contexto:
                abrir_conexion("r1", ajustes=self.ajustes)

        self.assertEqual(contexto.exception.tipo, "autenticacion")
        self.assertNotIn(ENTORNO_PRUEBA["NETAGENT_R1_CLAVE"], str(contexto.exception))

    def test_error_de_conexion_sanitiza_la_clave(self):
        clave = ENTORNO_PRUEBA["NETAGENT_R1_CLAVE"]
        with patch(
            "herramientas.comun.ConnectHandler",
            side_effect=Exception(f"fallo de red con {clave}"),
        ):
            with self.assertRaises(ErrorHerramienta) as contexto:
                abrir_conexion("r1", ajustes=self.ajustes)

        self.assertEqual(contexto.exception.tipo, "conexion")
        self.assertNotIn(clave, contexto.exception.mensaje)

    def test_equipo_desconocido_no_abre_conexion(self):
        with patch("herramientas.comun.ConnectHandler") as mock:
            with self.assertRaises(ErrorConfiguracion):
                abrir_conexion("pc1", ajustes=self.ajustes)
        mock.assert_not_called()

    def test_credenciales_faltantes_indican_variables(self):
        with patch.dict(
            os.environ, {"NETAGENT_R1_USUARIO": "", "NETAGENT_R1_CLAVE": ""}
        ):
            with self.assertRaises(ErrorConfiguracion) as contexto:
                abrir_conexion("r1", ajustes=self.ajustes)

        mensaje = str(contexto.exception)
        self.assertIn("NETAGENT_R1_USUARIO", mensaje)
        self.assertIn("NETAGENT_R1_CLAVE", mensaje)

    def test_cerrar_conexion_ignora_errores(self):
        falsa = ConexionFalsa(fallo_al_cerrar=True)
        cerrar_conexion(falsa)  # no debe propagar la excepción
        self.assertTrue(falsa.desconectada)
        cerrar_conexion(None)  # tolera None

    def test_contexto_cierra_la_sesion_aunque_falle_el_cuerpo(self):
        falsa = ConexionFalsa()
        with patch("herramientas.comun.ConnectHandler", return_value=falsa):
            with self.assertRaises(RuntimeError):
                with conexion_dispositivo("r1", ajustes=self.ajustes):
                    raise RuntimeError("fallo simulado")
        self.assertTrue(falsa.desconectada)


if __name__ == "__main__":
    unittest.main()
