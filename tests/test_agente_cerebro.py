"""Pruebas de agente/cerebro.py: coordinación, aprobación y registro (sin red)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agente.catalogo import (
    ACTUALIZAR_COMENTARIO,
    CONSULTAR_INTERFACES,
    Catalogo,
    Herramienta,
    catalogo_por_defecto,
)
from agente.cerebro import (
    Cerebro,
    RegistroSesion,
    Respuesta,
    confirmar_en_consola,
)
from agente.interpretar import (
    ErrorProveedor,
    LlamadaHerramienta,
    RespuestaModelo,
)
from config.ajustes import cargar_ajustes
from tests.test_comun import ENTORNO_PRUEBA

COMENTARIO_DEMO = "P2-NetAgent-IA-Demo"

LECTURA_OK = {
    "ok": True,
    "equipo": "R1",
    "interfaces": [
        {"nombre": "ether1", "estado": "running", "ip": "", "comentario": "WAN"},
        {"nombre": "ether4", "estado": "down", "ip": "", "comentario": ""},
    ],
    "changed": False,
    "error": None,
}

ESCRITURA_OK = {
    "ok": True,
    "equipo": "R1",
    "interfaz": "ether4",
    "comentario_anterior": "",
    "comentario_solicitado": COMENTARIO_DEMO,
    "changed": True,
    "confirmado": True,
    "verificacion": {"ok": True, "comentario_leido": COMENTARIO_DEMO, "coincide": True},
    "respuesta_equipo": None,
    "error": None,
}


class ProveedorFalso:
    """Proveedor de IA simulado para el cerebro."""

    def __init__(self, respuesta=None, error=None) -> None:
        self.respuesta = respuesta or RespuestaModelo(texto="hola")
        self.error = error
        self.llamadas: list[str] = []

    def responder(self, mensaje, herramientas):
        self.llamadas.append(mensaje)
        if self.error is not None:
            raise self.error
        return self.respuesta


class ProveedorCambioFalso(ProveedorFalso):
    """Repite la propuesta tras observar el diagnóstico, luego responde."""
    def responder_historial(self, mensajes, herramientas):
        self.llamadas.append(mensajes)
        if mensajes[-1]["role"] == "user":
            return self.respuesta
        observacion = json.loads(mensajes[-1]["content"])
        if observacion.get("ejecutada") is False:
            llamada = self.respuesta.llamadas[0]
            return RespuestaModelo(llamadas=(LlamadaHerramienta(llamada.nombre, llamada.argumentos, "cambio"),))
        return RespuestaModelo(texto="Verificación recibida")


class FuncionFalsa:
    """Herramienta simulada que registra sus invocaciones."""

    def __init__(self, salida, error=None) -> None:
        self.salida = salida
        self.error = error
        self.llamadas: list[dict] = []

    def __call__(self, **kwargs):
        self.llamadas.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return dict(self.salida)


def respuesta_lectura(equipo="r1"):
    return RespuestaModelo(
        llamadas=(LlamadaHerramienta(CONSULTAR_INTERFACES, {"equipo": equipo}),)
    )


def respuesta_escritura(comentario=COMENTARIO_DEMO, contrarios=None):
    argumentos = {"equipo": "r1", "interfaz": "ether4", "comentario": comentario}
    argumentos.update(contrarios or {})
    return RespuestaModelo(
        llamadas=(LlamadaHerramienta(ACTUALIZAR_COMENTARIO, argumentos),)
    )


class BaseCerebro(unittest.TestCase):
    def setUp(self):
        parche = patch.dict(os.environ, ENTORNO_PRUEBA)
        parche.start()
        self.addCleanup(parche.stop)
        self.ajustes = cargar_ajustes()
        directorio = tempfile.TemporaryDirectory()
        self.addCleanup(directorio.cleanup)
        self.directorio = Path(directorio.name)
        self.eventos: list[str] = []

    def construir(self, respuesta, funciones, confirmar=None, ajustes=None):
        """Cerebro con proveedor y catálogo simulados y registro en un temporal."""
        real = catalogo_por_defecto()
        funciones = dict(funciones)
        es_cambio = bool(respuesta.llamadas and respuesta.llamadas[0].nombre == ACTUALIZAR_COMENTARIO)
        if es_cambio and CONSULTAR_INTERFACES not in funciones:
            def consultar_falsa(**kwargs):
                datos = [dict(interfaz) for interfaz in LECTURA_OK["interfaces"]]
                cambio = funciones[ACTUALIZAR_COMENTARIO]
                if cambio.llamadas and cambio.error is None and cambio.salida.get("ok"):
                    datos[1]["comentario"] = cambio.salida.get("comentario_solicitado", "")
                return {**LECTURA_OK, "interfaces": datos}
            funciones[CONSULTAR_INTERFACES] = consultar_falsa
        catalogo = Catalogo(
            [
                Herramienta(definicion=real.obtener(nombre).definicion, funcion=funcion)
                for nombre, funcion in funciones.items()
            ]
        )
        self.proveedor = ProveedorCambioFalso(respuesta) if es_cambio else ProveedorFalso(respuesta)
        registro = RegistroSesion(
            directorio=self.directorio, modelo=self.ajustes.modelo
        )
        cerebro = Cerebro(
            ajustes or self.ajustes,
            proveedor=self.proveedor,
            catalogo=catalogo,
            salida=self.eventos.append,
            confirmar=confirmar or (lambda plan: False),
            registro=registro,
        )
        return cerebro

    @staticmethod
    def sin_conexion():
        return patch(
            "herramientas.comun.ConnectHandler",
            side_effect=AssertionError("el cerebro no debe abrir SSH directamente"),
        )


class PruebaFlujoLectura(BaseCerebro):
    def test_consulta_muestra_el_resultado_sin_pedir_aprobacion(self):
        funcion = FuncionFalsa(LECTURA_OK)
        preguntas: list[object] = []

        def confirmar(plan):
            preguntas.append(plan)
            return True

        cerebro = self.construir(
            respuesta_lectura(), {CONSULTAR_INTERFACES: funcion}, confirmar=confirmar
        )
        with self.sin_conexion():
            respuesta = cerebro.procesar("Analiza R1")

        self.assertEqual(respuesta.tipo, "consulta")
        self.assertTrue(respuesta.ok)
        self.assertIn("Interfaces de R1", respuesta.texto)
        self.assertIn("ether4", respuesta.texto)
        self.assertIn("running", respuesta.texto)
        self.assertEqual(preguntas, [])  # la lectura no pide aprobación
        self.assertEqual(len(funcion.llamadas), 1)
        self.assertNotIn("confirmacion", funcion.llamadas[0])
        self.assertIs(funcion.llamadas[0]["ajustes"], self.ajustes)
        self.assertEqual(cerebro.pasos_usados, 1)

    def test_consulta_fallida_se_informa_sin_inventar_datos(self):
        funcion = FuncionFalsa(
            {
                "ok": False,
                "equipo": "R1",
                "interfaces": [],
                "changed": False,
                "error": {"tipo": "conexion", "mensaje": "sin ruta al equipo"},
            }
        )
        cerebro = self.construir(respuesta_lectura(), {CONSULTAR_INTERFACES: funcion})
        respuesta = cerebro.procesar("Analiza R1")

        self.assertEqual(respuesta.tipo, "error")
        self.assertFalse(respuesta.ok)
        self.assertIn("No se pudo completar la consulta", respuesta.texto)
        self.assertIn("sin ruta al equipo", respuesta.texto)


class PruebaFlujoEscritura(BaseCerebro):
    def test_modificacion_con_aprobacion(self):
        funcion = FuncionFalsa(ESCRITURA_OK)

        def confirmar(plan):
            self.eventos.append("APROBAR")
            return True

        cerebro = self.construir(
            respuesta_escritura(), {ACTUALIZAR_COMENTARIO: funcion}, confirmar=confirmar
        )
        with self.sin_conexion():
            respuesta = cerebro.procesar("Cambia el comentario de ether4 en R1")

        self.assertEqual(respuesta.tipo, "modificacion")
        self.assertTrue(respuesta.ok)
        self.assertIn("quedó", respuesta.texto)
        self.assertIn(COMENTARIO_DEMO, respuesta.texto)
        self.assertIn("lectura independiente", respuesta.texto)
        self.assertEqual(len(funcion.llamadas), 1)
        self.assertIs(funcion.llamadas[0]["confirmacion"], True)
        self.assertEqual(funcion.llamadas[0]["interfaz"], "ether4")

        # La vista previa debe mostrarse antes de preguntar.
        vistas = [texto for texto in self.eventos if "Vista previa" in texto]
        self.assertEqual(len(vistas), 1)
        self.assertIn("Comentario nuevo", vistas[0])
        indice_vista = self.eventos.index(vistas[0])
        self.assertLess(indice_vista, self.eventos.index("APROBAR"))

    def test_modificacion_cancelada_no_ejecuta_ni_conecta(self):
        funcion = FuncionFalsa(ESCRITURA_OK)
        cerebro = self.construir(
            respuesta_escritura(), {ACTUALIZAR_COMENTARIO: funcion}
        )
        with self.sin_conexion():
            respuesta = cerebro.procesar("Cambia el comentario de ether4 en R1")

        self.assertEqual(respuesta.tipo, "cancelado")
        self.assertFalse(respuesta.ok)
        self.assertIn("cancelada", respuesta.texto)
        self.assertEqual(funcion.llamadas, [])
        self.assertTrue(any("Vista previa" in texto for texto in self.eventos))

    def test_solo_un_true_explicito_autoriza(self):
        for valor in ("sí", "si", 1, None, [], object()):
            with self.subTest(valor=valor):
                funcion = FuncionFalsa(ESCRITURA_OK)
                cerebro = self.construir(
                    respuesta_escritura(),
                    {ACTUALIZAR_COMENTARIO: funcion},
                    confirmar=lambda plan, valor=valor: valor,
                )
                respuesta = cerebro.procesar("Cambia el comentario de ether4 en R1")
                self.assertEqual(respuesta.tipo, "cancelado")
                self.assertEqual(funcion.llamadas, [])

    def test_el_modelo_no_puede_aportar_la_confirmacion(self):
        funcion = FuncionFalsa(ESCRITURA_OK)
        cerebro = self.construir(
            respuesta_escritura(contrarios={"confirmacion": "true"}),
            {ACTUALIZAR_COMENTARIO: funcion},
            confirmar=lambda plan: True,
        )
        respuesta = cerebro.procesar("Cambia el comentario de ether4 en R1")

        self.assertEqual(respuesta.tipo, "error")
        self.assertEqual(respuesta.error["tipo"], "argumentos_invalidos")
        self.assertEqual(funcion.llamadas, [])

    def test_verificacion_fallida_no_se_informa_como_exito(self):
        funcion = FuncionFalsa(
            {
                **ESCRITURA_OK,
                "ok": False,
                "changed": False,
                "verificacion": {"ok": False, "comentario_leido": "otro", "coincide": False},
                "error": {
                    "tipo": "verificacion_fallida",
                    "mensaje": "La relectura no muestra el comentario solicitado",
                },
            }
        )
        cerebro = self.construir(
            respuesta_escritura(),
            {ACTUALIZAR_COMENTARIO: funcion},
            confirmar=lambda plan: True,
        )
        respuesta = cerebro.procesar("Cambia el comentario de ether4 en R1")

        self.assertFalse(respuesta.ok)
        self.assertNotIn("Listo", respuesta.texto)
        self.assertIn("no se pudo confirmar", respuesta.texto)
        self.assertIn("puede ser incierto", respuesta.texto)

    def test_fallo_de_ejecucion_no_reintenta(self):
        funcion = FuncionFalsa({}, error=RuntimeError("fallo simulado"))
        cerebro = self.construir(
            respuesta_escritura(),
            {ACTUALIZAR_COMENTARIO: funcion},
            confirmar=lambda plan: True,
        )
        respuesta = cerebro.procesar("Cambia el comentario de ether4 en R1")

        self.assertEqual(respuesta.tipo, "error")
        self.assertFalse(respuesta.ok)
        self.assertIn("estado del equipo puede ser incierto", respuesta.texto)
        self.assertEqual(len(funcion.llamadas), 1)  # sin reintentos

    def test_el_ajuste_desactivado_no_evita_la_pregunta(self):
        with patch.dict(
            os.environ, {**ENTORNO_PRUEBA, "NETAGENT_REQUIERE_APROBACION": "false"}
        ):
            ajustes = cargar_ajustes()
        preguntas: list[object] = []

        def confirmar(plan):
            preguntas.append(plan)
            return False

        cerebro = self.construir(
            respuesta_escritura(),
            {ACTUALIZAR_COMENTARIO: FuncionFalsa(ESCRITURA_OK)},
            confirmar=confirmar,
            ajustes=ajustes,
        )
        respuesta = cerebro.procesar("Cambia el comentario de ether4 en R1")

        self.assertFalse(ajustes.requiere_aprobacion)
        self.assertEqual(len(preguntas), 1)  # se preguntó igualmente
        self.assertEqual(respuesta.tipo, "cancelado")

    def test_comentario_invalido_del_modelo_se_rechaza_antes_de_ejecutar(self):
        funcion = FuncionFalsa(ESCRITURA_OK)
        cerebro = self.construir(
            respuesta_escritura(comentario='demo"; /system reboot'),
            {ACTUALIZAR_COMENTARIO: funcion},
            confirmar=lambda plan: True,
        )
        respuesta = cerebro.procesar("Cambia el comentario")

        self.assertEqual(respuesta.error["tipo"], "parametros_invalidos")
        self.assertEqual(funcion.llamadas, [])
        self.assertEqual(self.eventos, [])  # ni siquiera hay vista previa


class PruebaErroresYLimites(BaseCerebro):
    def test_mensaje_conversacional(self):
        cerebro = self.construir(
            RespuestaModelo(texto="R1 es el router de borde."),
            {CONSULTAR_INTERFACES: FuncionFalsa(LECTURA_OK)},
        )
        respuesta = cerebro.procesar("¿Qué es R1?")

        self.assertEqual(respuesta.tipo, "mensaje")
        self.assertTrue(respuesta.ok)
        self.assertEqual(respuesta.texto, "R1 es el router de borde.")

    def test_herramienta_fuera_del_catalogo(self):
        funcion = FuncionFalsa(LECTURA_OK)
        cerebro = self.construir(
            RespuestaModelo(llamadas=(LlamadaHerramienta("reiniciar_equipo", {"equipo": "r1"}),)),
            {CONSULTAR_INTERFACES: funcion},
        )
        respuesta = cerebro.procesar("Reinicia R1")

        self.assertEqual(respuesta.tipo, "error")
        self.assertEqual(respuesta.error["tipo"], "herramienta_no_autorizada")
        self.assertEqual(funcion.llamadas, [])

    def test_error_del_proveedor_se_informa_una_sola_vez(self):
        funcion = FuncionFalsa(LECTURA_OK)
        cerebro = self.construir(
            RespuestaModelo(texto="x"),
            {CONSULTAR_INTERFACES: funcion},
        )
        proveedor = ProveedorFalso(error=ErrorProveedor("conexion", "sin Internet"))
        cerebro._proveedor = proveedor  # inyección del fallo del proveedor

        respuesta = cerebro.procesar("Analiza R1")

        self.assertEqual(respuesta.error["tipo"], "conexion")
        self.assertIn("No se pudo conectar", respuesta.texto)
        self.assertEqual(proveedor.llamadas, ["Analiza R1"])
        self.assertEqual(funcion.llamadas, [])

    def test_mensaje_vacio_no_consume_paso(self):
        cerebro = self.construir(
            RespuestaModelo(texto="hola"),
            {CONSULTAR_INTERFACES: FuncionFalsa(LECTURA_OK)},
        )
        respuesta = cerebro.procesar("   ")
        self.assertEqual(respuesta.error["tipo"], "parametros_invalidos")
        self.assertEqual(cerebro.pasos_usados, 0)

    def test_respeta_el_maximo_de_pasos(self):
        with patch.dict(
            os.environ, {**ENTORNO_PRUEBA, "NETAGENT_MAX_PASOS": "2"}
        ):
            ajustes = cargar_ajustes()
        cerebro = self.construir(
            RespuestaModelo(texto="hola"),
            {CONSULTAR_INTERFACES: FuncionFalsa(LECTURA_OK)},
            ajustes=ajustes,
        )

        self.assertTrue(cerebro.procesar("uno").ok)
        self.assertTrue(cerebro.procesar("dos").ok)
        tercera = cerebro.procesar("tres")

        self.assertFalse(tercera.ok)
        self.assertEqual(tercera.error["tipo"], "max_pasos_alcanzado")
        self.assertIn("máximo de 2 pasos", tercera.texto)
        self.assertEqual(len(cerebro._proveedor.llamadas), 2)
        self.assertEqual(cerebro.pasos_restantes, 0)


class PruebaRegistro(BaseCerebro):
    def test_la_sesion_genera_json_con_el_recorrido(self):
        funcion = FuncionFalsa(ESCRITURA_OK)
        cerebro = self.construir(
            respuesta_escritura(),
            {ACTUALIZAR_COMENTARIO: funcion},
            confirmar=lambda plan: True,
        )
        cerebro.procesar("Cambia el comentario de ether4 en R1")

        ruta = cerebro.ruta_registro
        self.assertIsNotNone(ruta)
        contenido = json.loads(Path(ruta).read_text(encoding="utf-8"))
        self.assertEqual(contenido["modelo"], self.ajustes.modelo)
        eventos = contenido["eventos"]
        self.assertGreaterEqual(len(eventos), 5)
        self.assertEqual(eventos[0]["paso"], 1)
        self.assertEqual(eventos[0]["usuario"], "Cambia el comentario de ether4 en R1")
        propuesta = next(e for e in eventos if e.get("nota", "").startswith("propuesta:"))
        self.assertEqual(propuesta["intencion"]["herramienta"], ACTUALIZAR_COMENTARIO)
        self.assertEqual(propuesta["plan"]["accion"]["argumentos"]["interfaz"], "ether4")
        ejecucion = next(e for e in eventos if e.get("nota") == "ejecución de escritura")
        self.assertTrue(ejecucion["resultado"]["aprobada"])
        self.assertIsNone(cerebro.error_registro)

    def test_el_registro_no_contiene_secretos(self):
        funcion = FuncionFalsa(ESCRITURA_OK)
        cerebro = self.construir(
            respuesta_escritura(),
            {ACTUALIZAR_COMENTARIO: funcion},
            confirmar=lambda plan: True,
        )
        cerebro.procesar("Cambia el comentario de ether4 en R1")

        texto = Path(cerebro.ruta_registro).read_text(encoding="utf-8")
        for secreto in (
            ENTORNO_PRUEBA["NETAGENT_R1_CLAVE"],
            ENTORNO_PRUEBA["NETAGENT_SW1_CLAVE"],
            ENTORNO_PRUEBA["NETAGENT_R1_USUARIO"],
            "10.60.30.202",
            "OPENAI_API_KEY",
        ):
            with self.subTest(secreto=secreto):
                self.assertNotIn(secreto, texto)

    def test_el_registro_oculta_campos_sensibles_y_recorta_textos(self):
        registro = RegistroSesion(directorio=self.directorio, modelo="modelo-prueba")
        registro.anotar(
            {
                "usuario": "hola",
                "api_key": "sk-prohibida",
                "password": "secreto",
                "clave": "r1",
                "detalle": {"credenciales": {"usuario": "admin", "clave": "x"}},
                "nota": "y" * 800,
            }
        )

        datos = json.loads(registro.ruta.read_text(encoding="utf-8"))
        evento = datos["eventos"][0]
        self.assertEqual(evento["api_key"], "<oculto>")
        self.assertEqual(evento["password"], "<oculto>")
        self.assertEqual(evento["clave"], "r1")  # clave del equipo, no un secreto
        self.assertEqual(evento["detalle"]["credenciales"], "<oculto>")
        self.assertEqual(len(evento["nota"]), 500)

    def test_un_fallo_de_escritura_no_interrumpe_la_sesion(self):
        archivo = self.directorio / "no-es-un-directorio"
        archivo.write_text("x", encoding="utf-8")
        registro = RegistroSesion(directorio=archivo, modelo="modelo-prueba")
        registro.anotar({"usuario": "hola"})

        self.assertIsNotNone(registro.error)
        self.assertFalse(registro.existe)

        cerebro = Cerebro(
            self.ajustes,
            proveedor=ProveedorFalso(RespuestaModelo(texto="hola")),
            catalogo=catalogo_por_defecto(),
            salida=self.eventos.append,
            confirmar=lambda plan: False,
            registro=RegistroSesion(directorio=archivo / "imposible", modelo="m"),
        )
        respuesta = cerebro.procesar("¿Qué es R1?")
        self.assertTrue(respuesta.ok)
        self.assertIsNotNone(cerebro.error_registro)
        self.assertEqual(len(self.eventos), 1)
        self.assertIn("no se pudo escribir el registro", self.eventos[0])


class PruebaConfirmacionEnConsola(unittest.TestCase):
    def _preguntar(self, entrada):
        with patch("builtins.input", return_value=entrada):
            return confirmar_en_consola(None)

    def test_acepta_si_y_s(self):
        for entrada in ("s", "S", " si ", "sí", "SI"):
            with self.subTest(entrada=entrada):
                self.assertTrue(self._preguntar(entrada))

    def test_cualquier_otra_cosa_es_no(self):
        for entrada in ("", "n", "no", "mejor no", "sí pero", "s1"):
            with self.subTest(entrada=entrada):
                self.assertFalse(self._preguntar(entrada))

    def test_eof_o_ctrl_c_se_tratan_como_cancelar(self):
        for error in (EOFError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with patch("builtins.input", side_effect=error):
                    self.assertFalse(confirmar_en_consola(None))


class PruebaRespuesta(unittest.TestCase):
    def test_estructura_serializable(self):
        respuesta = Respuesta(tipo="consulta", texto="ok", ok=True)
        datos = respuesta.a_dict()
        self.assertEqual(list(datos), ["tipo", "texto", "ok", "resultado", "error"])
        json.dumps(datos)


if __name__ == "__main__":
    unittest.main()
