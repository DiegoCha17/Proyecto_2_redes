"""Ansible Vault con datos sintéticos y sin conexiones de red."""
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import ejecutar_con_vault as lanzador

DATOS = '''r1:
  usuario: prueba_r1
  clave: clave_r1_prueba
sw1:
  usuario: prueba_sw1
  clave: clave_sw1_prueba
'''


def test_ruta_operativa_vault_y_configurador_coinciden():
    from scripts import configurar_vault
    assert lanzador.VAULT == lanzador.RAIZ / 'ansible/vault/vault.yml'
    assert configurar_vault.VAULT == lanzador.VAULT


def test_vault_rechaza_texto_plano_sin_invocar_ansible(tmp_path):
    ruta = tmp_path / 'datos.yml'
    ruta.write_text(DATOS)
    with patch.object(lanzador.subprocess, 'run') as ejecutar:
        with pytest.raises(lanzador.ErrorVault, match='no está cifrado'):
            lanzador.credenciales_vault(ruta, {'ANSIBLE_VAULT_PASSWORD_FILE': str(tmp_path / 'pass')})
    ejecutar.assert_not_called()


@pytest.mark.parametrize('contenido', [
    'r1: {usuario: a, clave: b}\n',
    'r1: {usuario: a, clave: b}\nsw1: {usuario: a, clave: ""}\n',
    'r1: {usuario: a, clave: b}\nr1: {usuario: x, clave: y}\nsw1: {usuario: c, clave: d}\n',
])
def test_esquema_invalido_no_devuelve_credenciales(tmp_path, contenido):
    ruta = tmp_path / 'datos.yml'
    ruta.write_text('$ANSIBLE_VAULT;1.1;AES256\ncontenido-cifrado')
    proceso = SimpleNamespace(returncode=0, stdout=contenido, stderr='')
    with patch.object(lanzador.subprocess, 'run', return_value=proceso):
        with pytest.raises(lanzador.ErrorVault):
            lanzador.credenciales_vault(ruta, {'ANSIBLE_VAULT_PASSWORD_FILE': str(tmp_path / 'pass')})


def test_password_maestra_no_puede_estar_en_repositorio(tmp_path):
    with pytest.raises(lanzador.ErrorVault, match='fuera del repositorio'):
        lanzador.credenciales_vault(tmp_path / 'datos', {'ANSIBLE_VAULT_PASSWORD_FILE': str(lanzador.RAIZ / '.vault-pass')})


def test_prioridad_vault_para_python_y_ansible_sin_cambiar_entorno_padre():
    credenciales = {'NETAGENT_R1_USUARIO': 'vault_r1', 'NETAGENT_R1_CLAVE': 'vault_r1_clave',
                    'NETAGENT_SW1_USUARIO': 'vault_sw1', 'NETAGENT_SW1_CLAVE': 'vault_sw1_clave'}
    with patch.dict(os.environ, {'NETAGENT_R1_USUARIO': 'entorno_anterior'}, clear=True):
        with patch.object(lanzador, 'credenciales_vault', return_value=credenciales):
            with patch.object(lanzador.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as ejecutar:
                assert lanzador.main(['--', 'python', 'main.py']) == 0
                entorno = ejecutar.call_args.kwargs['env']
                assert all(entorno[k] == v for k, v in credenciales.items())
                assert entorno['ANSIBLE_CONFIG'].endswith('ansible/ansible.cfg')
                assert os.environ['NETAGENT_R1_USUARIO'] == 'entorno_anterior'
                assert ejecutar.call_args.args[0] == ['python', 'main.py']


def test_fallo_vault_no_muestra_salida_sensible(tmp_path, capsys):
    ruta = tmp_path / 'datos.yml'
    ruta.write_text('$ANSIBLE_VAULT;1.1;AES256\ncontenido-cifrado')
    with patch.object(lanzador, 'VAULT', ruta), patch.dict(os.environ, {'ANSIBLE_VAULT_PASSWORD_FILE': str(tmp_path / 'pass')}, clear=True):
        with patch.object(lanzador.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='secreto_falso', stderr='secreto_falso')):
            assert lanzador.main(['--', 'python', 'main.py']) == 1
    assert 'secreto_falso' not in capsys.readouterr().err


def test_lectura_usa_decrypt_exacto_en_vez_de_view(tmp_path):
    ruta = tmp_path / 'datos.yml'
    ruta.write_text('$ANSIBLE_VAULT;1.1;AES256\ncontenido-cifrado')
    def ejecutar(comando, **kwargs):
        assert comando[1] == 'decrypt'
        assert comando[comando.index('--output') + 1] == '-'
        assert 'ANSIBLE_VAULT_PASSWORD_FILE' not in kwargs['env']
        return SimpleNamespace(returncode=0, stdout=DATOS, stderr='')
    with patch.object(lanzador.subprocess, 'run', side_effect=ejecutar):
        resultado = lanzador.credenciales_vault(ruta, {'ANSIBLE_VAULT_PASSWORD_FILE': str(tmp_path / 'pass')})
    assert resultado['NETAGENT_R1_CLAVE'] == 'clave_r1_prueba'


@pytest.mark.skipif(not shutil.which('ansible-vault'), reason='Ansible Vault no instalado')
def test_descifrado_vault_real_con_datos_sinteticos(tmp_path):
    password = tmp_path / 'password'
    password.write_text('master_sintetica\n')
    ruta = tmp_path / 'vault.yml'
    entorno = os.environ.copy()
    entorno['ANSIBLE_LOCAL_TEMP'] = str(tmp_path)
    proceso = subprocess.run(['ansible-vault', 'encrypt', '--vault-password-file', str(password),
                             '--output', str(ruta), '-'], input=DATOS, text=True,
                            env=entorno, capture_output=True, timeout=30)
    assert proceso.returncode == 0, 'No se pudo crear el fixture cifrado sintético'
    entorno['ANSIBLE_VAULT_PASSWORD_FILE'] = str(password)
    valores = lanzador.credenciales_vault(ruta, entorno)
    assert valores == {'NETAGENT_R1_USUARIO': 'prueba_r1', 'NETAGENT_R1_CLAVE': 'clave_r1_prueba',
                       'NETAGENT_SW1_USUARIO': 'prueba_sw1', 'NETAGENT_SW1_CLAVE': 'clave_sw1_prueba'}
    assert 'clave_r1_prueba' not in ruta.read_text()


@pytest.mark.skipif(not shutil.which('ansible-vault'), reason='Ansible Vault no instalado')
def test_descifrado_exacto_si_credencial_contiene_contrasena_maestra(tmp_path):
    password = tmp_path / 'password'
    password.write_text('pru42\n')
    ruta = tmp_path / 'vault.yml'
    entorno = os.environ.copy()
    entorno.pop('ANSIBLE_VAULT_PASSWORD_FILE', None)
    entorno['ANSIBLE_LOCAL_TEMP'] = str(tmp_path)
    datos = 'r1:\n  usuario: prueba_r1\n  clave: pru42\nsw1:\n  usuario: prueba_sw1\n  clave: adminpru42\n'
    proceso = subprocess.run(['ansible-vault', 'encrypt', '--vault-password-file', str(password),
                              '--output', str(ruta), '-'], input=datos, text=True,
                             env=entorno, capture_output=True, timeout=30)
    assert proceso.returncode == 0
    entorno['ANSIBLE_VAULT_PASSWORD_FILE'] = str(password)
    valores = lanzador.credenciales_vault(ruta, entorno)
    assert valores['NETAGENT_R1_CLAVE'] == 'pru42'
    assert valores['NETAGENT_SW1_CLAVE'] == 'adminpru42'


@pytest.mark.skipif(not shutil.which('ansible-playbook'), reason='Ansible no instalado')
@pytest.mark.parametrize('modo', ['entorno', 'vault'])
def test_ansible_resuelve_credenciales_separadas_sin_red(tmp_path, modo):
    inventario = tmp_path / 'inventory.ini'
    inventario.write_text('[mikrotik]\nr1 prefijo_env=NETAGENT_R1\nsw1 prefijo_env=NETAGENT_SW1\n')
    playbook = tmp_path / 'playbook.yml'
    variables = lanzador.RAIZ / 'ansible/group_vars/mikrotik.yml'
    playbook.write_text('''---
- hosts: all
  gather_facts: false
  vars_files:
    - "''' + str(variables) + '''"
  tasks:
    - name: Validar credenciales sin mostrarlas
      ansible.builtin.assert:
        that:
          - ansible_user == ('prueba_' + inventory_hostname)
          - ansible_password == ('clave_' + inventory_hostname + '_prueba')
      no_log: true
''')
    entorno = os.environ.copy()
    entorno['ANSIBLE_LOCAL_TEMP'] = str(tmp_path)
    credenciales = {'NETAGENT_R1_USUARIO': 'prueba_r1', 'NETAGENT_R1_CLAVE': 'clave_r1_prueba',
                    'NETAGENT_SW1_USUARIO': 'prueba_sw1', 'NETAGENT_SW1_CLAVE': 'clave_sw1_prueba'}
    if modo == 'vault':
        ruta = tmp_path / 'vault.yml'
        ruta.write_text('$ANSIBLE_VAULT;1.1;AES256\ncontenido-cifrado')
        with patch.object(lanzador.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=DATOS, stderr='')):
            credenciales = lanzador.credenciales_vault(ruta, {'ANSIBLE_VAULT_PASSWORD_FILE': str(tmp_path / 'pass')})
    entorno.update(credenciales)
    proceso = subprocess.run(['ansible-playbook', '-i', str(inventario), str(playbook)],
                             env=entorno, cwd=lanzador.RAIZ, capture_output=True, text=True, timeout=45)
    assert proceso.returncode == 0, 'La resolución de credenciales de Ansible falló'
    assert not any(valor in proceso.stdout + proceso.stderr for valor in credenciales.values())
