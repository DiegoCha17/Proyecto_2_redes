"""Selección de proveedores y compatibilidad local, sin conexiones reales."""

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agente.catalogo import CONSULTAR_INTERFACES
from agente.interpretar import ErrorProveedor, ProveedorOpenAI, interpretar
from config.ajustes import ErrorConfiguracion, cargar_ajustes
from main import main
from tests.test_agente_interpretar import ClienteFalso, llamada_sdk, mensaje_sdk
from tests.test_comun import ENTORNO_PRUEBA


class PruebaConfiguracionProveedores(unittest.TestCase):
    def ajustes(self, entorno):
        with patch.dict(os.environ, entorno, clear=True), patch("config.ajustes.load_dotenv"):
            return cargar_ajustes()

    def test_ollama_es_predeterminado_sin_clave(self):
        with patch.dict(os.environ, ENTORNO_PRUEBA, clear=True), patch("config.ajustes.load_dotenv"):
            ajustes = cargar_ajustes()
            self.assertEqual(ajustes.proveedor_ia, "ollama")
            self.assertEqual(ajustes.modelo, "qwen3:4b")
            self.assertEqual(ajustes.ollama_base_url, "http://127.0.0.1:11434/v1")
            self.assertNotIn("OPENAI_API_KEY", ajustes.variables_entorno_faltantes())

    def test_openai_exige_clave_y_conserva_modelo_predeterminado(self):
        with patch.dict(os.environ, {"NETAGENT_PROVEEDOR": "openai"}, clear=True), patch("config.ajustes.load_dotenv"):
            ajustes = cargar_ajustes()
            self.assertEqual(ajustes.modelo, "gpt-4o-mini")
            self.assertIn("OPENAI_API_KEY", ajustes.variables_entorno_faltantes())

    def test_modelo_y_url_personalizados(self):
        ajustes = self.ajustes({"NETAGENT_MODELO": "modelo-local", "NETAGENT_OLLAMA_BASE_URL": "http://localhost:11434/v1"})
        self.assertEqual(ajustes.modelo, "modelo-local")
        self.assertEqual(ajustes.ollama_base_url, "http://localhost:11434/v1")

    def test_proveedor_desconocido_falla_cerrado(self):
        with self.assertRaises(ErrorConfiguracion):
            self.ajustes({"NETAGENT_PROVEEDOR": "desconocido"})

    def test_cli_arranca_con_ollama_sin_clave(self):
        ajustes = self.ajustes(ENTORNO_PRUEBA)
        salida = io.StringIO()
        with patch.dict(os.environ, ENTORNO_PRUEBA, clear=True), patch("main.obtener_ajustes", return_value=ajustes), patch("builtins.input", return_value="/salir"), redirect_stdout(salida):
            self.assertEqual(main(), 0)
        self.assertIn("ollama", salida.getvalue())
        self.assertIn("qwen3:4b", salida.getvalue())


class PruebaClientesProveedores(unittest.TestCase):
    def test_historial_con_resultado_de_herramienta_se_transmite_al_sdk(self):
        cliente = ClienteFalso(mensaje_sdk(texto="Lectura recibida"))
        proveedor = ProveedorOpenAI("modelo-prueba", cliente=cliente)
        historial = [
            {"role": "user", "content": "Consulta R1"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-1", "type": "function", "function": {
                    "name": "consultar_interfaces", "arguments": '{"equipo":"r1"}'
                }}
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
        ]
        respuesta = proveedor.responder_historial(historial, [])
        self.assertEqual(respuesta.texto, "Lectura recibida")
        self.assertEqual(cliente.peticiones[0]["messages"], historial)

    def test_conserva_identificador_de_llamada_del_sdk(self):
        llamada = llamada_sdk(CONSULTAR_INTERFACES, {"equipo": "r1"})
        llamada.id = "call-1"
        proveedor = ProveedorOpenAI("modelo-prueba", cliente=ClienteFalso(mensaje_sdk(llamadas=[llamada])))
        self.assertEqual(proveedor.responder("Consulta R1", []).llamadas[0].id, "call-1")

    def test_ollama_envia_catalogo_y_tool_call_sin_usar_clave_openai(self):
        respuesta = mensaje_sdk(llamadas=[llamada_sdk(CONSULTAR_INTERFACES, {"equipo": "r1"})])
        respuesta.choices[0].message.reasoning_content = "contenido interno"
        cliente = ClienteFalso(respuesta)
        with patch.dict(os.environ, ENTORNO_PRUEBA, clear=True), patch("config.ajustes.load_dotenv"), patch("openai.OpenAI", return_value=cliente) as constructor:
            ajustes = cargar_ajustes()
            intencion = interpretar("Consulta R1", ajustes=ajustes)
        constructor.assert_called_once_with(api_key="ollama", base_url="http://127.0.0.1:11434/v1", timeout=120, max_retries=0)
        self.assertEqual(intencion.herramienta, CONSULTAR_INTERFACES)
        self.assertEqual(intencion.argumentos["equipo"], "r1")
        self.assertNotIn("contenido interno", intencion.mensaje)
        self.assertEqual(cliente.peticiones[0]["reasoning_effort"], "none")
        self.assertEqual(cliente.peticiones[0]["max_tokens"], 1024)
        self.assertEqual(cliente.peticiones[0]["tool_choice"], "auto")
        self.assertTrue(cliente.peticiones[0]["messages"][1]["content"].endswith("/no_think"))

    def test_ollama_descarta_razonamiento_en_content_y_conserva_llamada(self):
        respuesta = mensaje_sdk(texto="razonamiento privado</think>Respuesta final", llamadas=[llamada_sdk(CONSULTAR_INTERFACES, {"equipo": "r1"})])
        proveedor = ProveedorOpenAI("qwen3:4b", cliente=ClienteFalso(respuesta), desactivar_razonamiento=True)
        resultado = proveedor.responder("Consulta R1", [])
        self.assertEqual(resultado.texto, "Respuesta final")
        self.assertEqual(resultado.llamadas[0].nombre, CONSULTAR_INTERFACES)

    def test_ollama_rechaza_razonamiento_sin_cerrar(self):
        proveedor = ProveedorOpenAI("qwen3:4b", cliente=ClienteFalso(mensaje_sdk(texto="<think>privado")), desactivar_razonamiento=True)
        with self.assertRaises(ErrorProveedor):
            proveedor.responder("Hola", [])

    def test_openai_utiliza_clave_y_no_envia_opciones_de_ollama(self):
        cliente = ClienteFalso(mensaje_sdk(texto="Hola"))
        entorno = {**ENTORNO_PRUEBA, "NETAGENT_PROVEEDOR": "openai", "OPENAI_API_KEY": "clave-de-prueba"}
        with patch.dict(os.environ, entorno, clear=True), patch("config.ajustes.load_dotenv"), patch("openai.OpenAI", return_value=cliente) as constructor:
            ajustes = cargar_ajustes()
            intencion = interpretar("Hola", ajustes=ajustes)
        constructor.assert_called_once_with(api_key="clave-de-prueba")
        self.assertEqual(intencion.mensaje, "Hola")
        self.assertEqual(cliente.peticiones[0]["model"], "gpt-4o-mini")
        self.assertNotIn("reasoning_effort", cliente.peticiones[0])
        self.assertNotIn("max_tokens", cliente.peticiones[0])

    def test_limites_ollama_configurables(self):
        cliente = ClienteFalso(mensaje_sdk(texto="Hola"))
        entorno = {"NETAGENT_OLLAMA_TIMEOUT": "45", "NETAGENT_OLLAMA_MAX_TOKENS": "512"}
        with patch.dict(os.environ, entorno, clear=True), patch("config.ajustes.load_dotenv"), patch("openai.OpenAI", return_value=cliente) as constructor:
            ajustes = cargar_ajustes()
            interpretar("Hola", ajustes=ajustes)
        self.assertEqual(constructor.call_args.kwargs["timeout"], 45)
        self.assertEqual(constructor.call_args.kwargs["max_retries"], 0)
        self.assertEqual(cliente.peticiones[0]["max_tokens"], 512)

    def test_respuesta_truncada_rechaza_incluso_tool_call(self):
        respuesta = mensaje_sdk(llamadas=[llamada_sdk(CONSULTAR_INTERFACES, {"equipo": "r1"})])
        respuesta.choices[0].finish_reason = "length"
        proveedor = ProveedorOpenAI("qwen3:4b", cliente=ClienteFalso(respuesta), max_tokens=64)
        with self.assertRaises(ErrorProveedor) as contexto:
            proveedor.responder("Consulta R1", [])
        self.assertEqual(contexto.exception.tipo, "respuesta_incompleta")

    def test_timeout_es_error_tipado_sin_reintento_de_aplicacion(self):
        class APITimeoutError(Exception):
            pass
        cliente = ClienteFalso(error=APITimeoutError("detalle interno"))
        proveedor = ProveedorOpenAI("qwen3:4b", cliente=cliente)
        with self.assertRaises(ErrorProveedor) as contexto:
            proveedor.responder("Consulta R1", [])
        self.assertEqual(contexto.exception.tipo, "tiempo_agotado")
        self.assertNotIn("detalle interno", contexto.exception.mensaje)
        self.assertEqual(len(cliente.peticiones), 1)


if __name__ == "__main__":
    unittest.main()
