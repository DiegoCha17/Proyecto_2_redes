"""Recorridos completos con proveedor y herramientas simulados."""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from agente.catalogo import ACTUALIZAR_COMENTARIO, CONSULTAR_INTERFACES, Catalogo, Herramienta, catalogo_por_defecto
from agente.cerebro import Cerebro, RegistroSesion
from agente.interpretar import ErrorProveedor, LlamadaHerramienta, RespuestaModelo
from config.ajustes import cargar_ajustes
from tests.test_comun import ENTORNO_PRUEBA
from tests.test_ciclo_lectura import ProveedorSimulado, llamada


def proponer(id="cambio", comentario="nuevo"):
    return RespuestaModelo(llamadas=(LlamadaHerramienta(ACTUALIZAR_COMENTARIO,
        {"equipo": "r1", "interfaz": "ether4", "comentario": comentario}, id),))


class PruebaCicloModificacion(unittest.TestCase):
    def setUp(self):
        entorno = patch.dict(os.environ, ENTORNO_PRUEBA, clear=True)
        entorno.start()
        self.addCleanup(entorno.stop)
        with patch("config.ajustes.load_dotenv"):
            self.ajustes = cargar_ajustes()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.estado = "anterior"
        self.estado_interfaz = "running"
        self.lecturas = 0
        self.escrituras = []
        self.vistas = []
        self.preguntas = []
        self.error_lectura = False
        self.error_escritura = False
        self.no_aplicar = False

    def leer(self, **kwargs):
        self.lecturas += 1
        if self.error_lectura:
            return {"ok": False, "error": {"tipo": "conexion", "mensaje": "detalle privado"}}
        return {"ok": True, "interfaces": [{"nombre": "ether4", "estado": self.estado_interfaz,
                "comentario": self.estado, "ip": "10.60.30.202/24"}], "error": None}

    def escribir(self, **kwargs):
        self.escrituras.append(kwargs)
        if self.error_escritura:
            return {"ok": False, "error": {"tipo": "conexion", "mensaje": "privado"}}
        if not self.no_aplicar:
            self.estado = kwargs["comentario"]
        return {"ok": True, "changed": True, "verificacion": {"ok": True, "coincide": True}, "error": None}

    def construir(self, respuestas, confirmar=True, limite=6):
        real = catalogo_por_defecto()
        catalogo = Catalogo([Herramienta(real.obtener(CONSULTAR_INTERFACES).definicion, self.leer),
                             Herramienta(real.obtener(ACTUALIZAR_COMENTARIO).definicion, self.escribir)])
        proveedor = ProveedorSimulado(respuestas)
        def aprobar(plan):
            self.preguntas.append(plan)
            return confirmar
        registro = RegistroSesion(directorio=self.temp.name)
        cerebro = Cerebro(replace(self.ajustes, max_herramientas_por_solicitud=limite),
            proveedor=proveedor, catalogo=catalogo, confirmar=aprobar,
            salida=self.vistas.append, registro=registro)
        return cerebro, proveedor, registro

    def test_diagnostico_aprobacion_ejecucion_y_verificacion(self):
        cerebro, proveedor, registro = self.construir([llamada("r1", "lectura"), proponer(), RespuestaModelo(texto="Confirmado")])
        respuesta = cerebro.procesar("Cambia el comentario a nuevo")
        self.assertTrue(respuesta.ok)
        self.assertEqual(self.lecturas, 2)
        self.assertEqual(len(self.escrituras), 1)
        self.assertTrue(self.escrituras[0]["confirmacion"])
        self.assertEqual(len(self.preguntas), 1)
        self.assertTrue(any("anterior" in v and "nuevo" in v and "Justificación" in v for v in self.vistas))
        resultado = json.loads(proveedor.peticiones[-1][-1]["content"])
        self.assertTrue(resultado["verificacion"]["ok"])
        self.assertEqual(resultado["verificacion"]["interfaces"][0]["comentario"], "nuevo")
        notas = [e.get("nota", "") for e in registro.eventos]
        self.assertTrue(any("propuesta" in n for n in notas))
        self.assertTrue(any("aprobación explícita" in n for n in notas))
        self.assertTrue(any("verificación independiente" in n for n in notas))

    def test_cambio_directo_primero_diagnostica_y_vuelve_a_preguntar_al_modelo(self):
        cerebro, proveedor, _ = self.construir([proponer("primera"), proponer("segunda"), RespuestaModelo(texto="ok")])
        self.assertTrue(cerebro.procesar("Cambia el comentario").ok)
        self.assertEqual(self.lecturas, 2)
        self.assertFalse(json.loads(proveedor.peticiones[1][-1]["content"])["ejecutada"])

    def test_propuesta_inicial_mantiene_decision_tras_diagnostico(self):
        self.estado_interfaz = "down"
        cerebro, proveedor, _ = self.construir([
            proponer("primera"), RespuestaModelo(texto="Se requiere autorización."),
            proponer("segunda")], confirmar=False)
        respuesta = cerebro.procesar("Actualiza la descripción solicitada")
        self.assertEqual(respuesta.tipo, "cancelado")
        self.assertEqual(self.escrituras, [])
        self.assertEqual(self.lecturas, 1)
        self.assertEqual(len(proveedor.peticiones), 3)

    def test_rechazo_no_escribe(self):
        cerebro, _, _ = self.construir([llamada("r1", "lectura"), proponer()], confirmar=False)
        self.assertEqual(cerebro.procesar("Cambia el comentario").tipo, "cancelado")
        self.assertEqual(self.escrituras, [])
        self.assertEqual(self.lecturas, 1)

    def test_down_continua_diagnostico_y_cancelacion_humana(self):
        self.estado_interfaz = "down"
        cerebro, proveedor, registro = self.construir([
            llamada("r1", "lectura"),
            RespuestaModelo(texto="Se requiere una evaluación humana antes de proceder."),
            proponer()], confirmar=False)
        respuesta = cerebro.procesar("Cambia el comentario de ether4 de R1 a nuevo")
        self.assertEqual(respuesta.tipo, "cancelado")
        self.assertEqual(self.escrituras, [])
        self.assertEqual(self.lecturas, 1)
        self.assertEqual(len(self.preguntas), 1)
        self.assertTrue(any("anterior" in v and "nuevo" in v and "Justificación" in v for v in self.vistas))
        self.assertEqual(json.loads(proveedor.peticiones[1][-1]["content"])["interfaces"][0]["estado"], "down")
        self.assertIn("herramienta", proveedor.peticiones[2][-1]["content"])
        self.assertTrue(any("reconsideración" in e.get("nota", "") for e in registro.eventos))

    def test_down_aprobacion_y_verificacion_independiente(self):
        self.estado_interfaz = "down"
        cerebro, _, _ = self.construir([
            llamada("r1", "lectura"), RespuestaModelo(texto="Necesito autorización."),
            proponer(), RespuestaModelo(texto="Resultado verificado.")])
        respuesta = cerebro.procesar("Cambia el comentario de ether4 de R1 a nuevo")
        self.assertTrue(respuesta.ok)
        self.assertEqual(self.lecturas, 2)
        self.assertEqual(len(self.escrituras), 1)
        self.assertIs(self.escrituras[0]["confirmacion"], True)
        self.assertIn("lectura independiente", respuesta.texto)

    def test_down_idempotencia_no_ejecuta_escritura(self):
        self.estado_interfaz = "down"
        self.estado = "nuevo"
        cerebro, _, _ = self.construir([llamada("r1", "lectura"), proponer()])
        respuesta = cerebro.procesar("Cambia el comentario de ether4 de R1 a nuevo")
        self.assertTrue(respuesta.ok)
        self.assertIn("ya coincide", respuesta.texto)
        self.assertEqual(self.escrituras, [])
        self.assertEqual(self.preguntas, [])

    def test_reconsideracion_no_obliga_accion_y_es_unica(self):
        self.estado_interfaz = "down"
        cerebro, proveedor, _ = self.construir([
            llamada("r1", "lectura"), RespuestaModelo(texto="No procede."),
            RespuestaModelo(texto="Mantengo mi decisión.")])
        respuesta = cerebro.procesar("Cambia el comentario de ether4 de R1 a nuevo")
        self.assertEqual(respuesta.texto, "Mantengo mi decisión.")
        self.assertEqual(len(proveedor.peticiones), 3)
        self.assertEqual(self.escrituras, [])
        self.assertEqual(self.preguntas, [])

    def test_diagnostico_fallido_no_pide_reconsideracion_ni_escribe(self):
        self.error_lectura = True
        cerebro, proveedor, _ = self.construir([llamada("r1", "lectura"),
                                                RespuestaModelo(texto="Sin datos.")])
        respuesta = cerebro.procesar("Cambia el comentario de ether4 de R1 a nuevo")
        self.assertFalse(respuesta.ok)
        self.assertEqual(len(proveedor.peticiones), 2)
        self.assertEqual(self.escrituras, [])
        self.assertEqual(self.preguntas, [])

    def test_idempotencia_no_pregunta_ni_escribe(self):
        self.estado = "nuevo"
        cerebro, _, _ = self.construir([llamada("r1", "lectura"), proponer()])
        respuesta = cerebro.procesar("Cambia el comentario")
        self.assertTrue(respuesta.ok)
        self.assertIn("ya coincide", respuesta.texto)
        self.assertEqual(self.escrituras, [])
        self.assertEqual(self.preguntas, [])

    def test_verificacion_independiente_fallida_no_informa_exito(self):
        self.no_aplicar = True
        cerebro, proveedor, _ = self.construir([llamada("r1", "lectura"), proponer(),
                                                RespuestaModelo(texto="Cambio aplicado")])
        respuesta = cerebro.procesar("Cambia el comentario")
        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "verificacion_fallida")
        self.assertEqual(self.lecturas, 2)
        self.assertNotIn("Cambio aplicado", respuesta.texto)
        self.assertFalse(json.loads(proveedor.peticiones[-1][-1]["content"])["coincide"])

    def test_error_de_escritura_verifica_y_no_reintenta(self):
        self.error_escritura = True
        cerebro, proveedor, _ = self.construir([llamada("r1", "lectura"), proponer(),
                                                RespuestaModelo(texto="Hecho")])
        respuesta = cerebro.procesar("Cambia el comentario")
        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "ejecucion_fallida")
        self.assertEqual(len(self.escrituras), 1)
        self.assertEqual(self.lecturas, 2)
        self.assertFalse(json.loads(proveedor.peticiones[-1][-1]["content"])["ejecucion"]["ok"])

    def test_timeout_final_mantiene_estado_verificado_y_no_repite_cambio(self):
        cerebro, _, _ = self.construir([])
        class ProveedorConTimeout(ProveedorSimulado):
            def responder_historial(self, mensajes, herramientas):
                if len(self.peticiones) == 2:
                    raise ErrorProveedor("tiempo_agotado", "El proveedor de IA no respondió a tiempo")
                return super().responder_historial(mensajes, herramientas)
        cerebro._proveedor = ProveedorConTimeout([llamada("r1", "lectura"), proponer()])
        respuesta = cerebro.procesar("Cambia el comentario")
        self.assertFalse(respuesta.ok)
        self.assertEqual(respuesta.error["tipo"], "tiempo_agotado")
        self.assertIn("lectura independiente lo confirmó", respuesta.texto)
        self.assertIn("no respondió a tiempo", respuesta.texto)
        self.assertEqual(len(self.escrituras), 1)
        self.assertEqual(self.lecturas, 2)

    def test_timeout_tras_diagnostico_no_solicita_aprobacion_ni_escribe(self):
        cerebro, _, _ = self.construir([])
        class ProveedorConTimeout(ProveedorSimulado):
            def responder_historial(self, mensajes, herramientas):
                if self.peticiones:
                    raise ErrorProveedor("tiempo_agotado", "El proveedor de IA no respondió a tiempo")
                return super().responder_historial(mensajes, herramientas)
        cerebro._proveedor = ProveedorConTimeout([llamada("r1", "lectura")])
        respuesta = cerebro.procesar("Cambia el comentario")
        self.assertEqual(respuesta.error["tipo"], "tiempo_agotado")
        self.assertEqual(self.escrituras, [])
        self.assertEqual(self.preguntas, [])

    def test_timeout_al_explicar_verificacion_fallida_se_informa(self):
        self.no_aplicar = True
        cerebro, _, _ = self.construir([])
        class ProveedorConTimeout(ProveedorSimulado):
            def responder_historial(self, mensajes, herramientas):
                if len(self.peticiones) == 2:
                    raise ErrorProveedor("tiempo_agotado", "El proveedor de IA no respondió a tiempo")
                return super().responder_historial(mensajes, herramientas)
        cerebro._proveedor = ProveedorConTimeout([llamada("r1", "lectura"), proponer()])
        respuesta = cerebro.procesar("Cambia el comentario")
        self.assertEqual(respuesta.error["tipo"], "verificacion_fallida")
        self.assertIn("no respondió a tiempo", respuesta.texto)
        self.assertEqual(len(self.escrituras), 1)
        self.assertEqual(self.lecturas, 2)

    def test_no_ejecuta_segundo_cambio_tras_verificar(self):
        cerebro, _, _ = self.construir([llamada("r1", "lectura"), proponer(), proponer("repetida")])
        respuesta = cerebro.procesar("Cambia el comentario")
        self.assertEqual(respuesta.error["tipo"], "accion_repetida")
        self.assertEqual(len(self.escrituras), 1)

    def test_no_envia_ni_registra_credencial_en_comentario(self):
        self.estado = ENTORNO_PRUEBA["NETAGENT_R1_CLAVE"]
        cerebro, proveedor, registro = self.construir([llamada("r1", "lectura"), proponer()])
        respuesta = cerebro.procesar("Cambia el comentario")
        self.assertEqual(respuesta.error["tipo"], "diagnostico_fallido")
        self.assertNotIn(self.estado, json.dumps(proveedor.peticiones))
        self.assertNotIn(self.estado, json.dumps(registro.eventos))
        self.assertEqual(self.escrituras, [])

    def test_error_de_diagnostico_impide_escritura(self):
        self.error_lectura = True
        cerebro, _, _ = self.construir([proponer()])
        respuesta = cerebro.procesar("Cambia el comentario")
        self.assertEqual(respuesta.error["tipo"], "diagnostico_fallido")
        self.assertEqual(self.escrituras, [])

    def test_no_escribe_si_no_queda_presupuesto_para_verificar(self):
        cerebro, _, _ = self.construir([llamada("r1", "lectura"), proponer()], limite=2)
        respuesta = cerebro.procesar("Cambia el comentario")
        self.assertEqual(respuesta.error["tipo"], "limite_herramientas")
        self.assertEqual(self.escrituras, [])
        self.assertEqual(self.preguntas, [])

    def test_propuesta_en_sw1_se_rechaza(self):
        llamada_sw = RespuestaModelo(llamadas=(LlamadaHerramienta(ACTUALIZAR_COMENTARIO,
            {"equipo": "sw1", "interfaz": "ether4", "comentario": "nuevo"}, "sw"),))
        cerebro, _, _ = self.construir([llamada_sw])
        self.assertEqual(cerebro.procesar("Cambia SW1").error["tipo"], "interfaz_no_autorizada")
        self.assertEqual(self.escrituras, [])

    def test_alias_del_modelo_se_resuelve_solo_con_diagnostico_y_cancelacion(self):
        erronea = RespuestaModelo(llamadas=(LlamadaHerramienta(ACTUALIZAR_COMENTARIO,
            {"equipo": "r-1", "interfaz": "ether4", "comentario": "nuevo"}, "err"),))
        cerebro, _, registro = self.construir([llamada("r1", "lectura"), erronea], confirmar=False)
        respuesta = cerebro.procesar("Cambia el comentario de ether4 de R1 a nuevo")
        self.assertEqual(respuesta.tipo, "cancelado")
        self.assertEqual(self.escrituras, [])
        self.assertEqual(self.lecturas, 1)
        self.assertEqual(self.preguntas[0].accion.clave, "r1")
        self.assertTrue(any("alias r-1" in e.get("nota", "") for e in registro.eventos))

    def test_clave_inventada_nunca_se_corrige_por_codigo(self):
        erronea = RespuestaModelo(llamadas=(LlamadaHerramienta(ACTUALIZAR_COMENTARIO,
            {"equipo": "r-1", "interfaz": "ether4", "comentario": "nuevo"}, "err"),))
        cerebro, _, _ = self.construir([erronea], confirmar=True)
        respuesta = cerebro.procesar("Cambia el comentario de ether4 de R1 a nuevo")
        self.assertEqual(respuesta.error["tipo"], "dispositivo_no_autorizado")
        self.assertEqual(self.escrituras, [])
        self.assertEqual(self.lecturas, 0)

    def test_alias_resuelto_exige_aprobacion_y_verifica(self):
        erronea = RespuestaModelo(llamadas=(LlamadaHerramienta(ACTUALIZAR_COMENTARIO,
            {"equipo": "r-1", "interfaz": "ether4", "comentario": "nuevo"}, "err"),))
        cerebro, _, _ = self.construir([llamada("r1", "lectura"), erronea,
                                        RespuestaModelo(texto="Verificado")], confirmar=True)
        respuesta = cerebro.procesar("Cambia el comentario de ether4 de R1 a nuevo")
        self.assertTrue(respuesta.ok)
        self.assertEqual(self.lecturas, 2)
        self.assertEqual(len(self.escrituras), 1)
        self.assertEqual(self.escrituras[0]["equipo"], "r1")
        self.assertIs(self.escrituras[0]["confirmacion"], True)

    def test_alias_ambiguo_no_se_resuelve(self):
        erronea = RespuestaModelo(llamadas=(LlamadaHerramienta(ACTUALIZAR_COMENTARIO,
            {"equipo": "r-1", "interfaz": "ether4", "comentario": "nuevo"}, "err"),))
        cerebro, _, _ = self.construir([llamada("r1", "lectura"), erronea], confirmar=True)
        respuesta = cerebro.procesar("Cambia el comentario de ether4 de R1 o SW1 a nuevo")
        self.assertEqual(respuesta.error["tipo"], "dispositivo_no_autorizado")
        self.assertEqual(self.escrituras, [])


if __name__ == "__main__":
    unittest.main()
