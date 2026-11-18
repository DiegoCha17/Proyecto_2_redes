"""Pruebas de agente/interpretar.py sin llamadas reales al proveedor de IA."""

from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agente.catalogo import (
    ACTUALIZAR_COMENTARIO,
    CONSULTAR_INTERFACES,
    catalogo_por_defecto,
)
from agente.interpretar import (
    TIPO_HERRAMIENTA,
    TIPO_MENSAJE,
    ErrorProveedor,
    Intencion,
    LlamadaHerramienta,
    ProveedorOpenAI,
    RespuestaModelo,
    convertir_respuesta,
    interpretar,
)
from config.ajustes import cargar_ajustes
from herramientas.comun import ErrorHerramienta
from tests.test_comun import ENTORNO_PRUEBA


class ProveedorFalso:
    """Proveedor de IA simulado: devuelve una respuesta preparada."""

    def __init__(self, respuesta=None, error=None) -> None:
        self.respuesta = respuesta if respuesta is not None else RespuestaModelo(texto="hola")
        self.error = error
        self.llamadas: list[tuple[str, list]] = []

    def responder(self, mensaje, herramientas):
        self.llamadas.append((mensaje, list(herramientas)))
        if self.error is not None:
            raise self.error
        return self.respuesta


def mensaje_sdk(texto=None, llamadas=None):
    """Objeto con la forma de ``ChatCompletion`` del SDK de OpenAI."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=texto, tool_calls=llamadas)
            )
        ]
    )


def llamada_sdk(nombre, argumentos):
    crudo = argumentos if isinstance(argumentos, str) else json.dumps(argumentos)
    return SimpleNamespace(function=SimpleNamespace(name=nombre, arguments=crudo))


class ClienteFalso:
    """Cliente del SDK simulado; registra los parámetros de la petición."""

    def __init__(self, respuesta=None, error=None) -> None:
        self.respuesta = respuesta
        self.error = error
        self.peticiones: list[dict] = []
        crear = self._crear
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=crear))

    def _crear(self, **kwargs):
        self.peticiones.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.respuesta


class PruebaInterpretacion(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()

    def _interpretar(self, respuesta=None, error=None):
        proveedor = ProveedorFalso(respuesta=respuesta, error=error)
        intencion = interpretar(
            "Analiza R1", proveedor=proveedor, ajustes=self.ajustes
        )
        return intencion, proveedor

    def test_mensaje_conversacional_no_propone_ninguna_herramienta(self):
        intencion, _ = self._interpretar(RespuestaModelo(texto=" R1 es un router. "))
        self.assertEqual(intencion.tipo, TIPO_MENSAJE)
        self.assertIsNone(intencion.herramienta)
        self.assertEqual(intencion.mensaje, "R1 es un router.")
        self.assertEqual(dict(intencion.argumentos), {})

    def test_llamada_valida_se_convierte_en_intencion(self):
        respuesta = RespuestaModelo(
            texto="Consulto R1.",
            llamadas=(
                LlamadaHerramienta(CONSULTAR_INTERFACES, {"equipo": "r1"}),
            ),
        )
        intencion, _ = self._interpretar(respuesta)
        self.assertEqual(intencion.tipo, TIPO_HERRAMIENTA)
        self.assertEqual(intencion.herramienta, CONSULTAR_INTERFACES)
        self.assertEqual(dict(intencion.argumentos), {"equipo": "r1"})
        self.assertEqual(intencion.mensaje, "Consulto R1.")

    def test_solo_se_ofrecen_las_herramientas_del_catalogo(self):
        _, proveedor = self._interpretar(RespuestaModelo(texto="hola"))
        mensaje, herramientas = proveedor.llamadas[0]
        self.assertEqual(mensaje, "Analiza R1")
        self.assertEqual(
            [esquema["function"]["name"] for esquema in herramientas],
            list(catalogo_por_defecto().nombres()),
        )

    def test_herramienta_fuera_del_catalogo_se_rechaza(self):
        respuesta = RespuestaModelo(
            llamadas=(LlamadaHerramienta("reiniciar_equipo", {"equipo": "r1"}),)
        )
        with self.assertRaises(ErrorHerramienta) as contexto:
            interpretar("reinicia R1", proveedor=ProveedorFalso(respuesta), ajustes=self.ajustes)
        self.assertEqual(contexto.exception.tipo, "herramienta_no_autorizada")

    def test_argumentos_fuera_del_esquema_se_rechazan(self):
        casos = (
            {"equipo": "r1", "comando": "/system reboot"},
            {"equipo": "r1", "interfaz": "ether4"},
            {"equipo": 1},
        )
        for argumentos in casos:
            with self.subTest(argumentos=argumentos):
                respuesta = RespuestaModelo(
                    llamadas=(LlamadaHerramienta(ACTUALIZAR_COMENTARIO, argumentos),)
                )
                with self.assertRaises(ErrorHerramienta) as contexto:
                    interpretar(
                        "cambia", proveedor=ProveedorFalso(respuesta), ajustes=self.ajustes
                    )
                self.assertEqual(contexto.exception.tipo, "argumentos_invalidos")

    def test_mas_de_una_accion_en_un_paso_se_rechaza(self):
        respuesta = RespuestaModelo(
            llamadas=(
                LlamadaHerramienta(CONSULTAR_INTERFACES, {"equipo": "r1"}),
                LlamadaHerramienta(CONSULTAR_INTERFACES, {"equipo": "sw1"}),
            )
        )
        with self.assertRaises(ErrorProveedor) as contexto:
            interpretar("mira los dos", proveedor=ProveedorFalso(respuesta), ajustes=self.ajustes)
        self.assertEqual(contexto.exception.tipo, "respuesta_invalida")

    def test_error_del_proveedor_se_propaga_tipado(self):
        error = ErrorProveedor("conexion", "sin salida a Internet")
        with self.assertRaises(ErrorProveedor) as contexto:
            interpretar("Analiza R1", proveedor=ProveedorFalso(error=error), ajustes=self.ajustes)
        self.assertEqual(contexto.exception.tipo, "conexion")

    def test_mensaje_vacio_no_consulta_al_proveedor(self):
        proveedor = ProveedorFalso()
        for mensaje in ("", "   ", None):
            with self.subTest(mensaje=mensaje):
                with self.assertRaises(ErrorHerramienta) as contexto:
                    interpretar(mensaje, proveedor=proveedor, ajustes=self.ajustes)
                self.assertEqual(contexto.exception.tipo, "parametros_invalidos")
        self.assertEqual(proveedor.llamadas, [])

    def test_intencion_es_inmutable_y_serializable(self):
        intencion = Intencion(
            tipo=TIPO_HERRAMIENTA,
            herramienta=CONSULTAR_INTERFACES,
            argumentos={"equipo": "r1"},
        )
        with self.assertRaises(TypeError):
            intencion.argumentos["equipo"] = "sw1"
        json.dumps(intencion.a_dict())


class PruebaRespuestaDelSDK(unittest.TestCase):
    def test_tool_call_con_argumentos_json_se_deserializa(self):
        respuesta = convertir_respuesta(
            mensaje_sdk(llamadas=[llamada_sdk(CONSULTAR_INTERFACES, {"equipo": "sw1"})])
        )
        self.assertEqual(respuesta.llamadas[0].nombre, CONSULTAR_INTERFACES)
        self.assertEqual(dict(respuesta.llamadas[0].argumentos), {"equipo": "sw1"})

    def test_respuesta_sin_choices_o_sin_mensaje_es_invalida(self):
        casos = (
            SimpleNamespace(choices=[]),
            SimpleNamespace(choices=None),
            SimpleNamespace(choices=[SimpleNamespace(message=None)]),
            None,
        )
        for respuesta in casos:
            with self.subTest(respuesta=respuesta):
                with self.assertRaises(ErrorProveedor) as contexto:
                    convertir_respuesta(respuesta)
                self.assertEqual(contexto.exception.tipo, "respuesta_invalida")

    def test_contenido_o_argumentos_mal_formados_se_rechazan(self):
        casos = (
            mensaje_sdk(texto=["hola"]),
            mensaje_sdk(llamadas=[SimpleNamespace(function=SimpleNamespace(name="", arguments="{}"))]),
            mensaje_sdk(llamadas=[SimpleNamespace(function=None)]),
            mensaje_sdk(llamadas=[llamada_sdk(CONSULTAR_INTERFACES, "{no es json")]),
            mensaje_sdk(llamadas=[llamada_sdk(CONSULTAR_INTERFACES, "[1, 2]")]),
            mensaje_sdk(llamadas=[SimpleNamespace(function=SimpleNamespace(name="x", arguments=5))]),
        )
        for respuesta in casos:
            with self.subTest(respuesta=respuesta):
                with self.assertRaises(ErrorProveedor) as contexto:
                    convertir_respuesta(respuesta)
                self.assertEqual(contexto.exception.tipo, "respuesta_invalida")

    def test_sin_llamadas_devuelve_solo_texto(self):
        respuesta = convertir_respuesta(mensaje_sdk(texto="  hola  "))
        self.assertEqual(respuesta.texto, "hola")
        self.assertEqual(respuesta.llamadas, ())


class PruebaProveedorOpenAI(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, {**ENTORNO_PRUEBA, "OPENAI_API_KEY": "clave-prueba-ia"})
        parche.start()
        self.addCleanup(parche.stop)

    def test_usa_el_modelo_y_las_herramientas_del_catalogo(self):
        cliente = ClienteFalso(mensaje_sdk(texto="Hola"))
        proveedor = ProveedorOpenAI(modelo="modelo-prueba", cliente=cliente)

        respuesta = proveedor.responder("Analiza R1", catalogo_por_defecto().esquemas())

        self.assertEqual(respuesta.texto, "Hola")
        peticion = cliente.peticiones[0]
        self.assertEqual(peticion["model"], "modelo-prueba")
        self.assertEqual(
            [esquema["function"]["name"] for esquema in peticion["tools"]],
            list(catalogo_por_defecto().nombres()),
        )
        self.assertEqual(peticion["tool_choice"], "auto")
        self.assertEqual([m["role"] for m in peticion["messages"]], ["system", "user"])

    def test_no_envia_credenciales_ni_direcciones_de_gestion(self):
        cliente = ClienteFalso(mensaje_sdk(texto="Hola"))
        ProveedorOpenAI(modelo="modelo-prueba", cliente=cliente).responder(
            "Analiza R1", catalogo_por_defecto().esquemas()
        )
        enviado = json.dumps(cliente.peticiones[0], ensure_ascii=False)
        for prohibido in ("10.60.30.202", "10.60.30.204", "clave-prueba-ia",
                          ENTORNO_PRUEBA["NETAGENT_R1_CLAVE"], "NETAGENT_R1_USUARIO"):
            with self.subTest(prohibido=prohibido):
                self.assertNotIn(prohibido, enviado)

    def test_sin_api_key_el_error_es_tipado(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            proveedor = ProveedorOpenAI(modelo="modelo-prueba")
            with self.assertRaises(ErrorProveedor) as contexto:
                proveedor.responder("Analiza R1", [])
        self.assertEqual(contexto.exception.tipo, "configuracion")
        self.assertIn("OPENAI_API_KEY", contexto.exception.mensaje)

    def test_errores_del_sdk_se_traducen_sin_filtrar_detalles(self):
        class AuthenticationError(Exception):
            pass

        class RateLimitError(Exception):
            pass

        casos = (
            (AuthenticationError("sk-secreto-en-el-mensaje"), "autenticacion"),
            (RateLimitError("cuota agotada"), "limite_tasa"),
            (RuntimeError("fallo raro con sk-secreto-en-el-mensaje"), "proveedor"),
        )
        for error, esperado in casos:
            with self.subTest(error=type(error).__name__):
                cliente = ClienteFalso(error=error)
                proveedor = ProveedorOpenAI(modelo="modelo-prueba", cliente=cliente)
                with self.assertRaises(ErrorProveedor) as contexto:
                    proveedor.responder("Analiza R1", [])
                self.assertEqual(contexto.exception.tipo, esperado)
                self.assertNotIn("sk-secreto", contexto.exception.mensaje)

    def test_respuesta_invalida_del_cliente_se_rechaza(self):
        proveedor = ProveedorOpenAI(modelo="modelo-prueba", cliente=ClienteFalso(object()))
        with self.assertRaises(ErrorProveedor) as contexto:
            proveedor.responder("Analiza R1", [])
        self.assertEqual(contexto.exception.tipo, "respuesta_invalida")

    def test_cliente_inyectado_evita_crear_uno_real(self):
        cliente = ClienteFalso(mensaje_sdk(texto="Hola"))
        with patch("openai.OpenAI", side_effect=AssertionError("no debe crearse cliente")):
            ProveedorOpenAI(modelo="modelo-prueba", cliente=cliente).responder("hola", [])
        self.assertEqual(len(cliente.peticiones), 1)


if __name__ == "__main__":
    unittest.main()
