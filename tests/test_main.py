"""Pruebas de la terminal conversacional (main.py) sin conexiones reales."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from config.ajustes import ErrorConfiguracion, cargar_ajustes
from main import banner, main
from tests.test_comun import ENTORNO_PRUEBA

ENTORNO_CON_IA = {**ENTORNO_PRUEBA, "OPENAI_API_KEY": "clave-prueba-ia"}


class CerebroFalso:
    """Cerebro simulado: no interpreta, no conecta y no escribe."""

    def __init__(self, textos=None, error=None, ruta_registro=None) -> None:
        self.textos = list(textos or [])
        self.error = error
        self.ruta_registro = ruta_registro
        self.consultas: list[str] = []

    def procesar(self, mensaje):
        self.consultas.append(mensaje)
        if self.error is not None:
            raise self.error
        texto = self.textos.pop(0) if self.textos else "sin respuesta"
        return SimpleNamespace(texto=texto)


class PruebaBucle(unittest.TestCase):
    def _ejecutar(self, entradas, cerebro):
        buffer = io.StringIO()
        with patch("builtins.input", side_effect=entradas), redirect_stdout(buffer):
            codigo = main(cerebro=cerebro)
        return codigo, buffer.getvalue()

    def test_salir_termina_sin_consultar_al_agente(self):
        cerebro = CerebroFalso()
        codigo, salida = self._ejecutar(["/salir"], cerebro)

        self.assertEqual(codigo, 0)
        self.assertIn("Hasta luego.", salida)
        self.assertEqual(cerebro.consultas, [])

    def test_salir_admite_mayusculas(self):
        cerebro = CerebroFalso()
        codigo, salida = self._ejecutar(["/SALIR"], cerebro)
        self.assertEqual(codigo, 0)
        self.assertIn("Hasta luego.", salida)

    def test_una_consulta_se_imprime(self):
        cerebro = CerebroFalso(["Interfaces de R1 (2; 1 en estado running):"])
        codigo, salida = self._ejecutar(["Analiza R1", "/salir"], cerebro)

        self.assertEqual(codigo, 0)
        self.assertEqual(cerebro.consultas, ["Analiza R1"])
        self.assertIn("Interfaces de R1", salida)

    def test_varias_consultas_se_envian_en_orden(self):
        cerebro = CerebroFalso(["uno", "dos"])
        codigo, salida = self._ejecutar(["  Analiza R1  ", "¿Qué interfaces están desconectadas?", "/salir"], cerebro)

        self.assertEqual(codigo, 0)
        self.assertEqual(
            cerebro.consultas,
            ["Analiza R1", "¿Qué interfaces están desconectadas?"],
        )
        self.assertIn("uno", salida)
        self.assertIn("dos", salida)

    def test_entradas_vacias_se_ignoran(self):
        cerebro = CerebroFalso()
        codigo, _ = self._ejecutar(["", "   ", "/salir"], cerebro)
        self.assertEqual(codigo, 0)
        self.assertEqual(cerebro.consultas, [])

    def test_ctrl_c_en_el_prompt_cierra_con_mensaje(self):
        cerebro = CerebroFalso()
        codigo, salida = self._ejecutar([KeyboardInterrupt()], cerebro)

        self.assertEqual(codigo, 0)
        self.assertIn("Sesión interrumpida por el usuario.", salida)
        self.assertNotIn("Traceback", salida)

    def test_ctrl_d_cierra_con_mensaje(self):
        cerebro = CerebroFalso()
        codigo, salida = self._ejecutar([EOFError()], cerebro)

        self.assertEqual(codigo, 0)
        self.assertIn("Hasta luego.", salida)

    def test_error_inesperado_no_muestra_trazas_y_la_sesion_continua(self):
        cerebro = CerebroFalso(error=RuntimeError("fallo con clave-prueba-ia"))
        codigo, salida = self._ejecutar(["Analiza R1", "/salir"], cerebro)

        self.assertEqual(codigo, 0)
        self.assertEqual(len(cerebro.consultas), 1)
        self.assertNotIn("Traceback", salida)
        self.assertNotIn("clave-prueba-ia", salida)
        self.assertIn("Error inesperado", salida)

    def test_muestra_la_ruta_del_registro_al_terminar(self):
        with tempfile.TemporaryDirectory() as directorio:
            ruta = os.path.join(directorio, "sesion.json")
            cerebro = CerebroFalso(ruta_registro=ruta)
            codigo, salida = self._ejecutar(["/salir"], cerebro)

        self.assertEqual(codigo, 0)
        self.assertIn(ruta, salida)


class PruebaArranque(unittest.TestCase):
    def test_banner_no_expone_datos_sensibles(self):
        with patch.dict(os.environ, ENTORNO_CON_IA):
            ajustes = cargar_ajustes()
        texto = banner(ajustes)

        self.assertIn("NetAgent-IA", texto)
        self.assertIn("R1", texto)
        self.assertIn("SW1", texto)
        self.assertIn("ether4", texto)
        self.assertIn("/salir", texto)
        for prohibido in (
            "10.60.30.202",
            "10.60.30.204",
            ENTORNO_CON_IA["NETAGENT_R1_CLAVE"],
            ENTORNO_CON_IA["OPENAI_API_KEY"],
        ):
            with self.subTest(prohibido=prohibido):
                self.assertNotIn(prohibido, texto)

    def test_sin_configuracion_avisa_y_sale_con_error(self):
        buffer = io.StringIO()
        with patch("main.obtener_ajustes", side_effect=ErrorConfiguracion("falta el YAML")):
            with redirect_stdout(buffer):
                codigo = main()

        self.assertEqual(codigo, 1)
        self.assertIn("No se pudo cargar la configuración", buffer.getvalue())
        self.assertIn("falta el YAML", buffer.getvalue())

    def test_sin_api_key_avisa_y_sale_con_error(self):
        buffer = io.StringIO()
        with patch.dict(os.environ, {**ENTORNO_PRUEBA, "NETAGENT_PROVEEDOR": "openai", "OPENAI_API_KEY": ""}):
            with patch("main.obtener_ajustes", return_value=cargar_ajustes()), patch("builtins.input", side_effect=AssertionError("no debe pedir entrada")):
                with redirect_stdout(buffer):
                    codigo = main()

        salida = buffer.getvalue()
        self.assertEqual(codigo, 1)
        self.assertIn("OPENAI_API_KEY", salida)
        self.assertIn(".env", salida)

    def test_avisa_si_faltan_credenciales_ssh_pero_continua(self):
        buffer = io.StringIO()
        entorno = {"NETAGENT_SW1_USUARIO": "", "NETAGENT_SW1_CLAVE": ""}
        with patch.dict(os.environ, {**ENTORNO_CON_IA, **entorno}):
            with patch("builtins.input", side_effect=["/salir"]):
                with redirect_stdout(buffer):
                    codigo = main()

        salida = buffer.getvalue()
        self.assertEqual(codigo, 0)
        self.assertIn("faltan credenciales SSH", salida)
        self.assertIn("NETAGENT_SW1_CLAVE", salida)
        self.assertNotIn(ENTORNO_CON_IA["NETAGENT_R1_CLAVE"], salida)


if __name__ == "__main__":
    unittest.main()
