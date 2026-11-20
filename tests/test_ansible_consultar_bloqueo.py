"""Pruebas locales del procesamiento Ansible, sin acceder a equipos."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


RAIZ = Path(__file__).resolve().parents[1]
PLAYBOOK = RAIZ / "tests/ansible/consulta_bloqueo_local.yml"


@unittest.skipUnless(shutil.which("ansible-playbook"), "Ansible no está instalado")
class PruebaConsultaBloqueo(unittest.TestCase):
    def ejecutar_consulta(self, lineas):
        with tempfile.TemporaryDirectory() as temporal:
            entorno = os.environ.copy()
            entorno.update({
                "ANSIBLE_CONFIG": str(RAIZ / "ansible/ansible.cfg"),
                "ANSIBLE_LOCAL_TEMP": temporal,
                "ANSIBLE_STDOUT_CALLBACK": "ansible.posix.json",
                "ANSIBLE_NOCOLOR": "1",
                "ANSIBLE_FILTER_PLUGINS": str(RAIZ / "ansible/filter_plugins"),
            })
            return subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(PLAYBOOK),
                 "-e", json.dumps({"salida_simulada": "\n".join(lineas)})],
                cwd=RAIZ, env=entorno, capture_output=True, text=True, timeout=45,
            )

    def procesar(self, reglas):
        proceso = self.ejecutar_consulta(["NETAGENT_BEGIN_V1", *reglas, "NETAGENT_END_V1"])
        self.assertEqual(proceso.returncode, 0, proceso.stderr + proceso.stdout)
        salida = json.loads(proceso.stdout)
        resultados = [host["netagent_resultado"]
                      for play in salida["plays"] for task in play["tasks"]
                      for host in task.get("hosts", {}).values()
                      if "netagent_resultado" in host]
        self.assertEqual(len(resultados), 1)
        return resultados[0]

    def test_ausente(self):
        resultado = self.procesar([])
        self.assertFalse(resultado["regla_administrada_existente"])
        self.assertFalse(resultado["bloqueo_configurado"])
        self.assertIsNone(resultado["bloqueo_efectivo"])
        self.assertFalse(resultado["changed"])

    def test_existente_habilitada(self):
        resultado = self.procesar(["NETAGENT_RULE|forward|drop|192.168.10.50/32|172.16.100.10/32|false"])
        self.assertTrue(resultado["bloqueo_configurado"])
        self.assertTrue(resultado["reglas"][0]["habilitada"])
        self.assertIsNone(resultado["bloqueo_efectivo"])

    def test_deshabilitada(self):
        resultado = self.procesar(["NETAGENT_RULE|forward|drop|192.168.10.50|172.16.100.10|true"])
        self.assertTrue(resultado["regla_administrada_existente"])
        self.assertFalse(resultado["bloqueo_configurado"])
        self.assertFalse(resultado["reglas"][0]["habilitada"])

    def test_no_coincidente(self):
        resultado = self.procesar(["NETAGENT_RULE|forward|drop|192.168.10.60|172.16.100.10|no"])
        self.assertTrue(resultado["regla_administrada_existente"])
        self.assertFalse(resultado["bloqueo_configurado"])
        self.assertFalse(resultado["reglas"][0]["coincide_objetivo"])

    def test_accion_no_coincidente(self):
        resultado = self.procesar(["NETAGENT_RULE|forward|accept|192.168.10.50|172.16.100.10|no"])
        self.assertFalse(resultado["bloqueo_configurado"])

    def test_salida_incompleta_falla_cerrada(self):
        proceso = self.ejecutar_consulta(["NETAGENT_BEGIN_V1"])
        self.assertNotEqual(proceso.returncode, 0)
        self.assertNotIn('"netagent_resultado"', proceso.stdout)

    def test_formato_invalido_falla_cerrada(self):
        proceso = self.ejecutar_consulta(["NETAGENT_BEGIN_V1", "error", "NETAGENT_END_V1"])
        self.assertNotEqual(proceso.returncode, 0)
        self.assertNotIn('"netagent_resultado"', proceso.stdout)

    def test_eco_fragmentado_antes_del_bloque(self):
        proceso = self.ejecutar_consulta([
            '<IN_V1', '< filter find where comment="NetAgent-IA:CU02:PC1-WAN"]',
            '< firewall filter get $regla disabled]) }; :put "NETAGENT_END_V1"',
            'NETAGENT_BEGIN_V1',
            'NETAGENT_RULE|forward|drop|192.168.10.50|172.16.100.10|false',
            'NETAGENT_END_V1',
        ])
        self.assertEqual(proceso.returncode, 0, proceso.stderr + proceso.stdout)
        salida = json.loads(proceso.stdout)
        resultados = [host['netagent_resultado'] for play in salida['plays']
                      for task in play['tasks'] for host in task.get('hosts', {}).values()
                      if 'netagent_resultado' in host]
        self.assertEqual(len(resultados), 1)
        self.assertTrue(resultados[0]['bloqueo_configurado'])
        self.assertIsNone(resultados[0]['bloqueo_efectivo'])

    def test_eco_con_bloque_vacio(self):
        proceso = self.ejecutar_consulta(['<IN_V1', '< filter find...',
                                          'NETAGENT_BEGIN_V1', 'NETAGENT_END_V1'])
        self.assertEqual(proceso.returncode, 0, proceso.stderr + proceso.stdout)

    def test_rechaza_marcadores_duplicados_y_salida_posterior(self):
        for lineas in (
            ['NETAGENT_BEGIN_V1', 'NETAGENT_BEGIN_V1', 'NETAGENT_END_V1'],
            ['NETAGENT_BEGIN_V1', 'NETAGENT_END_V1', 'NETAGENT_END_V1'],
            ['NETAGENT_BEGIN_V1', 'NETAGENT_END_V1', 'error posterior'],
            ['error anterior', 'NETAGENT_BEGIN_V1', 'NETAGENT_END_V1'],
            ['<IN_V1', 'NETAGENT_BEGIN_V1', 'error', 'NETAGENT_END_V1'],
        ):
            with self.subTest(lineas=lineas):
                proceso = self.ejecutar_consulta(lineas)
                self.assertNotEqual(proceso.returncode, 0)
                self.assertNotIn('"netagent_resultado"', proceso.stdout)

    def regla_v2(self, adicionales=()):
        props = [('.id', '*A'), ('chain', 'forward'), ('action', 'drop'),
                 ('src-address', '192.168.10.50/32'), ('dst-address', '172.16.100.10/32'),
                 ('disabled', 'false'), ('comment', 'NetAgent-IA:CU02:PC1-WAN')]
        return ['NETAGENT_RULE_BEGIN_V2', *['NETAGENT_PROP|%s|%s' % p for p in props + list(adicionales)],
                'NETAGENT_RULE_END_V2']

    def test_regla_completa_valida(self):
        r = self.procesar(self.regla_v2([('dynamic', 'false'), ('invalid', 'no')]))
        self.assertTrue(r['segura_para_modificar'])
        self.assertTrue(r['reglas'][0]['regla_administrada_valida'])

    def test_restricciones_adicionales_bloquean_administracion(self):
        for campo in ('protocol', 'dst-port', 'in-interface', 'src-address-list', 'connection-state'):
            with self.subTest(campo=campo):
                r = self.procesar(self.regla_v2([(campo, 'tcp')]))
                self.assertFalse(r['segura_para_modificar'])
                self.assertIn(campo, r['reglas'][0]['propiedades_inesperadas'])

    def test_reglas_duplicadas_bloquean_administracion(self):
        r = self.procesar(self.regla_v2() + self.regla_v2())
        self.assertTrue(r['reglas_duplicadas'])
        self.assertFalse(r['segura_para_modificar'])

    def test_formato_anterior_no_autoriza_administracion(self):
        r = self.procesar(['NETAGENT_RULE|forward|drop|192.168.10.50|172.16.100.10|false'])
        self.assertFalse(r['segura_para_modificar'])
        self.assertFalse(r['reglas'][0]['inspeccion_completa'])

    def test_propiedad_duplicada_o_regla_incompleta_falla(self):
        for reglas in (self.regla_v2([('chain', 'forward')]), self.regla_v2()[:-1]):
            with self.subTest(reglas=reglas):
                p = self.ejecutar_consulta(['NETAGENT_BEGIN_V1', *reglas, 'NETAGENT_END_V1'])
                self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
